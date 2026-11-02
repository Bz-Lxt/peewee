"""
Diff database schema against model definitions.

Detects basic changes:

* tables to create
    Tables in the database that no model covers are ignored, and tables
    are never proposed for dropping.
* columns to add/drop
    Column diff does not introspect types/nullability/defaults/constraints.
    Without a rename mapping a renamed column is an add + drop.
* indexes to add/drop
    Index diff looks at columns being indexed and unique flag. Partial and
    expression indexes are compared by name only.
* tables and columns to rename
    Explicit rename mappings turn a model/table or field/column rename into
    a data-preserving ``RENAME`` instead of a drop plus a create. Renames
    pair bijectively, so swapping two names (``a`` -> ``b`` and ``b`` ->
    ``a``) is supported; the generated migration parks one name under a
    temporary one. Invalid mappings raise ``ValueError`` before anything
    is rendered or executed.

Usage::

    diff = diff_models(db, [User, Tweet])
    if diff:
        print(diff)

Renames are given from the *current* database names to the names the
models now declare::

    diff = diff_models(
        db, [Account, Tweet],
        table_renames={'sd_user': 'sd_account'},
        column_renames={'sd_tweet': {'user_id': 'author_id'}})

Schema-qualified tables are written as ``'schema.table'`` on the backends
that accept a schema (PostgreSQL, MySQL). SQLite has no schema support in
the migrator, so qualified keys are rejected.

Each attribute of the result maps directly onto a SchemaMigrator call:

* ``create_tables``: model classes (in dependency order)
* ``rename_tables``: :class:`TableRename`
* ``rename_columns``: :class:`ColumnRename`
* ``add_columns``: model fields to add
* ``drop_columns``: list of ``(table, column name)``
* ``add_indexes``: list of :class:`IndexDiff`
* ``drop_indexes``: list of :class:`IndexDiff`
"""
import itertools
import re
from collections import namedtuple

from peewee import *
from peewee import sort_models

__all__ = ['ColumnRename', 'IndexDiff', 'SchemaDiff', 'TableRename',
           'diff_models', 'plan_renames']


_IndexDiff = namedtuple('_IndexDiff', (
    'table',
    'name',
    'columns',
    'unique'))

class IndexDiff(_IndexDiff):
    __slots__ = ()

    def display(self, op):
        if self.columns is None:
            return '%s index %s.%s' % (op, self.table, self.name)
        return '%s index %s%s (%s)%s' % (
            op, self.table, '.%s' % self.name if self.name else '',
            ', '.join(self.columns), ' unique' if self.unique else '')


_TableRename = namedtuple('_TableRename', ('schema', 'old_name', 'new_name'))

class TableRename(_TableRename):
    __slots__ = ()

    def __str__(self):
        prefix = ('%s.' % self.schema) if self.schema else ''
        return 'rename table %s%s to %s' % (
            prefix, self.old_name, self.new_name)


_ColumnRename = namedtuple('_ColumnRename', (
    'schema',
    'table',
    'old_name',
    'new_name'))

class ColumnRename(_ColumnRename):
    __slots__ = ()

    def __str__(self):
        prefix = ('%s.' % self.schema) if self.schema else ''
        return 'rename column %s%s.%s to %s' % (
            prefix, self.table, self.old_name, self.new_name)


_SchemaDiff = namedtuple('_SchemaDiff', (
    'create_tables',
    'rename_tables',
    'rename_columns',
    'add_columns',
    'drop_columns',
    'add_indexes',
    'drop_indexes'))

class SchemaDiff(_SchemaDiff):
    __slots__ = ()

    def __bool__(self):
        return any(self)

    def __str__(self):
        accum = ['create table %s' % m._meta.table_name
                 for m in self.create_tables]
        accum.extend(str(r) for r in self.rename_tables)
        accum.extend(str(r) for r in self.rename_columns)
        accum.extend('add column %s.%s' % (f.model._meta.table_name,
                                           f.column_name)
                     for f in self.add_columns)
        accum.extend('drop column %s.%s' % tc for tc in self.drop_columns)
        accum.extend(idx.display('add') for idx in self.add_indexes)
        accum.extend(idx.display('drop') for idx in self.drop_indexes)
        return '\n'.join(accum)


def _model_indexes(model):
    # Plain indexes as a (columns, unique) signature. Partial and expression
    # indexes carry no signature, just their (deterministic) name.
    plain, named = set(), set()
    for index in model._meta.fields_to_index():
        if not isinstance(index, Index):
            continue  # SQL declarations are out of scope.
        parts = index._expressions
        if index._where is None and all(isinstance(p, Field) for p in parts):
            plain.add((tuple(p.column_name for p in parts),
                       bool(index._unique)))
        elif index._name:
            named.add(index._name)
    return plain, named


def _is_partial(index):
    return bool(index.sql and re.search(r'\)\s*WHERE\s', index.sql, re.I))


def _database_indexes(database, table, schema=None):
    # Every index as name -> (cols, unique) signature, or name -> None
    # when partial / expression. Primary-key indexes are excluded.
    indexes = {}
    for index in database.get_indexes(table, schema):
        if index.name == 'PRIMARY' or index.name.endswith('_pkey') or \
           index.name.startswith('sqlite_autoindex_'):
            continue
        if None in index.columns or _is_partial(index):
            indexes[index.name] = None
        else:
            indexes[index.name] = (tuple(index.columns), bool(index.unique))
    return indexes


def _split_qualified(value, label):
    # 'table' -> (None, 'table'), 'schema.table' -> ('schema', 'table').
    if not isinstance(value, str) or not value.strip():
        raise ValueError('%s must be a non-empty table name.' % label)
    parts = value.split('.')
    if len(parts) > 2 or any(not part for part in parts):
        raise ValueError('%s %r is not a valid table name.' % (label, value))
    if len(parts) == 2:
        return parts[0], parts[1]
    return None, parts[0]


def _check_identifier(name, label):
    if not isinstance(name, str) or not name or '.' in name:
        raise ValueError('%s %r must be a plain, non-empty identifier.'
                         % (label, name))
    return name


def plan_renames(pairs, occupied):
    """
    Order logical ``(old, new)`` renames into physical rename steps.

    A name that is free is taken directly. When every pending target is
    still occupied (a swap or longer cycle), one source is parked under a
    temporary name first. The returned steps run in order and leave every
    other occupant untouched; reversing them with swapped arguments gives
    the inverse.
    """
    pending = [(old, new) for old, new in pairs]
    state = set(occupied)
    steps = []
    counter = itertools.count()

    def fresh_name():
        while True:
            name = '__pw_rename_tmp_%s' % next(counter)
            if name not in state:
                return name

    while pending:
        remaining = []
        moved = False
        for old, new in pending:
            if new not in state:
                steps.append((old, new))
                state.discard(old)
                state.add(new)
                moved = True
            else:
                remaining.append((old, new))
        if moved:
            pending = remaining
            continue

        # Every destination is occupied: a cycle. Park one source, then
        # retry so the table/column it blocks can take its name.
        sources = {old for old, _ in pending}
        for old, new in pending:
            if new not in sources:
                raise ValueError(
                    'cannot rename %r to %r: the name is occupied and is '
                    'not renamed away.' % (old, new))
        old, new = pending[0]
        tmp = fresh_name()
        steps.append((old, tmp))
        state.discard(old)
        state.add(tmp)
        pending = [(tmp, new)] + pending[1:]

    return steps


def _prepare_renames(database, models, table_renames, column_renames):
    # Normalize and validate the user-supplied mappings against both the
    # model declarations and the live database. Raises ValueError before
    # the diff -- and therefore before any migration -- is produced.
    supports_schema = not isinstance(database, SqliteDatabase)

    # First model wins for a (schema, table), matching diff_models' loop.
    declared = {}
    for model in sort_models(models):
        meta = model._meta
        if getattr(meta, 'extension_module', None):
            continue
        declared.setdefault((meta.schema, meta.table_name), model)

    tables_cache = {}

    def live_tables(schema):
        if schema not in tables_cache:
            tables_cache[schema] = set(database.get_tables(schema))
        return tables_cache[schema]

    # -- table renames -----------------------------------------------------
    pairs = []           # (schema, old, new), validated logical pairs
    source_of = {}       # (schema, new) -> old
    pairset = set()
    raw_tables = sorted((table_renames or {}).items())
    for raw_old, raw_new in raw_tables:
        old_schema, old = _split_qualified(raw_old, 'rename source')
        _check_identifier(raw_new, 'rename target')
        new = raw_new

        if old_schema is not None and not supports_schema:
            raise ValueError('%s does not support schema-qualified table '
                             'names.' % type(database).__name__)

        if old_schema is None:
            # Resolve the schema through the declared target.
            candidates = {sch for sch, name in declared if name == new}
            if not candidates:
                raise ValueError(
                    'rename target %r is not declared by any model.' % new)
            if len(candidates) > 1:
                raise ValueError(
                    'rename target %r is declared in several schemas; '
                    'qualify the source as schema.%s.' % (new, new))
            schema = next(iter(candidates))
        else:
            schema = old_schema
            if (schema, new) not in declared:
                raise ValueError(
                    'rename target %s.%s is not declared by any model.'
                    % (schema, new))

        if old == new:
            raise ValueError('cannot rename table %s.%s to itself.'
                             % (schema, old))
        if (schema, old) in pairset:
            raise ValueError('table %s.%s is renamed twice.' % (schema, old))
        if (schema, new) in source_of:
            raise ValueError('two tables rename to %s.%s.' % (schema, new))
        if old not in live_tables(schema):
            raise ValueError('rename source table %s.%s does not exist in '
                             'the database.' % (schema, old))
        pairs.append((schema, old, new))
        pairset.add((schema, old))
        source_of[(schema, new)] = old

    # A target that currently exists must be renamed away itself, or the
    # rename lands on an occupied name (swaps/cycles are the valid case).
    for schema, old, new in pairs:
        if new in live_tables(schema) and (schema, new) not in pairset:
            raise ValueError(
                'cannot rename %s.%s to %s.%s: %s.%s exists and is not '
                'renamed away (map %s to %s as well to swap names).'
                % (schema, old, schema, new, schema, new,
                   new, old))

    # Each declared model resolves to one physical table.
    claimed = {}
    for schema, table in declared:
        physical = source_of.get((schema, table), table)
        key = (schema, physical)
        if key in claimed:
            raise ValueError(
                'tables %r and %r both resolve to physical table %s.%s.'
                % (claimed[key], table, schema, physical))
        claimed[key] = table

    # -- column renames ----------------------------------------------------
    sources = {(schema, old) for schema, old, _ in pairs}
    # Column maps are keyed by the current (physical) table name; the
    # declared model may sit under the rename's target name.
    new_of_source = {(schema, old): new
                     for schema, old, new in pairs}

    def resolve_physical(raw):
        schema, table = _split_qualified(raw, 'column-rename table')
        if schema is not None and not supports_schema:
            raise ValueError('%s does not support schema-qualified table '
                             'names.' % type(database).__name__)
        if schema is None:
            # Either a table-rename source or a self-matched existing
            # model table. The name must resolve to exactly one schema.
            candidates = set()
            for sch, name in sources:
                if name == table:
                    candidates.add(sch)
            for (sch, name), model in declared.items():
                if name == table and (sch, name) not in source_of and \
                        name in live_tables(sch):
                    candidates.add(sch)
            if not candidates:
                raise ValueError(
                    'column-rename table %r is not an existing table.'
                    % table)
            if len(candidates) > 1:
                raise ValueError(
                    'column-rename table %r matches tables in several '
                    'schemas; qualify it as schema.%s.' % (table, table))
            schema = next(iter(candidates))
        elif (schema, table) not in sources and \
                not ((schema, table) in declared and
                      (schema, table) not in source_of and
                      table in live_tables(schema)):
            raise ValueError(
                'column-rename table %s.%s is not an existing table.'
                % (schema, table))
        return schema, table

    colmaps = {}        # (schema, physical table) -> {old col: new col}
    raw_columns = sorted((column_renames or {}).items())
    for raw_table, mapping in raw_columns:
        if not isinstance(mapping, dict) or not mapping:
            raise ValueError('column renames for %r must be a non-empty '
                             'dict.' % raw_table)
        schema, physical = resolve_physical(raw_table)
        target = new_of_source.get((schema, physical), physical)
        model = declared[(schema, target)]

        db_columns = set(c.name for c in
                         database.get_columns(physical, schema))
        declared_columns = set(field.column_name
                               for field in model._meta.sorted_fields)

        renames = {}
        for raw_old, raw_new in sorted(mapping.items()):
            _check_identifier(raw_old, 'column-rename source')
            _check_identifier(raw_new, 'column-rename target')
            old, new = raw_old, raw_new
            if old == new:
                raise ValueError('cannot rename column %s.%s.%s to '
                                 'itself.' % (schema, physical, old))
            if old not in db_columns:
                raise ValueError(
                    'rename source column %s.%s.%s does not exist.'
                    % (schema, physical, old))
            if new not in declared_columns:
                raise ValueError(
                    'rename target column %s.%s.%s is not declared on '
                    'model %s.' % (schema, target, new, model.__name__))
            if new in set(renames.values()):
                raise ValueError('two columns of %s.%s rename to %r.'
                                 % (schema, physical, new))
            # Final-state collision: an existing target is only allowed
            # when it moves away itself (a swap).
            if new in db_columns and new not in mapping:
                raise ValueError(
                    'cannot rename %s.%s.%s to %r: that column already '
                    'exists and is not renamed away (map it as well to '
                    'swap names).' % (schema, physical, old, new))
            renames[old] = new
        colmaps[(schema, physical)] = renames

    by_target = {(r.schema, r.new_name): r
                 for r in (TableRename(schema, old, new)
                           for schema, old, new in pairs)}
    return by_target, source_of, colmaps


def diff_models(database, models, table_renames=None, column_renames=None):
    """
    Compare the database schema against the given models.

    :param table_renames: optional mapping of the current table names to
        the model-declared names, e.g. ``{'old_users': 'users'}``. Keys
        may be ``'schema.table'`` qualified.
    :param column_renames: optional mapping keyed by the *current* table
        name, of current column names to model-declared names, e.g.
        ``{'tweet': {'user_id': 'author_id'}}``.
    :raises ValueError: an unknown/ambiguous/colliding rename mapping,
        checked against the database before the diff is returned.
    :return: a `SchemaDiff` (falsy when everything matches).
    """
    rename_by_target, source_of, colmaps = _prepare_renames(
        database, models, table_renames, column_renames)

    create_tables = []
    rename_tables, rename_columns = [], []
    add_columns, drop_columns = [], []
    add_indexes, drop_indexes = [], []

    tables = {}  # Cache per schema.
    seen = set()
    for model in sort_models(models):
        meta = model._meta
        if getattr(meta, 'extension_module', None):
            continue  # Virtual tables (sqlite fts, etc).
        table, schema = meta.table_name, meta.schema
        if (schema, table) in seen:
            # Multiple models mapped to same table - skip.
            continue

        seen.add((schema, table))
        if schema not in tables:
            tables[schema] = set(database.get_tables(schema))

        physical = source_of.get((schema, table))
        if physical is None and table not in tables[schema]:
            create_tables.append(model)
            continue

        if physical is not None:
            source_table = physical
            rename_tables.append(rename_by_target[(schema, table)])
        else:
            source_table = table

        ren = colmaps.get((schema, source_table), {})

        columns = set(c.name for c in
                      database.get_columns(source_table, schema))
        fields = {field.column_name: field for field in meta.sorted_fields}
        # Read the database through the column-rename mapping, so a field
        # whose column was renamed pairs with its data instead of showing
        # up as add + drop.
        projected = {ren.get(name, name) for name in columns}
        add_columns.extend(field for name, field in fields.items()
                           if name not in projected)
        drop_columns.extend((table, name)
                            for name in sorted(columns)
                            if ren.get(name, name) not in fields)
        rename_columns.extend(
            ColumnRename(schema, table, old, new)
            for old, new in sorted(ren.items()))

        # Pair database indexes with declarations: named (partial and
        # expression) indexes by name, plain indexes by signature. Names
        # pair first, as the database may report a named index
        # (e.g. ts.desc()) as plain. Index columns follow the same rename
        # projection, so a composite index over a renamed column pairs
        # instead of being dropped and recreated.
        plain, named = _model_indexes(model)
        db_indexes = _database_indexes(database, source_table, schema)
        for name, signature in sorted(db_indexes.items()):
            if signature is not None:
                cols, unique = signature
                signature = (tuple(ren.get(col, col) for col in cols),
                             unique)
            if name in named:
                named.remove(name)
            elif signature in plain:
                plain.remove(signature)
            elif signature is None:
                drop_indexes.append(IndexDiff(table, name, None, None))
            else:
                cols, unique = signature
                drop_indexes.append(IndexDiff(table, name, cols, unique))

        for name in sorted(named):
            add_indexes.append(IndexDiff(table, name, None, None))
        for cols, unique in sorted(plain):
            add_indexes.append(IndexDiff(table, None, cols, unique))

    return SchemaDiff(create_tables, rename_tables, rename_columns,
                      add_columns, drop_columns, add_indexes, drop_indexes)
