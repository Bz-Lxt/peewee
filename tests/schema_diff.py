import datetime
import decimal
import enum
import shutil
import tempfile
import time
import uuid

from peewee import *
from playhouse.fields import EnumField
from playhouse.fields import IntEnumField
from playhouse.migrations import Runner
from playhouse.migrations import template
from playhouse.schema_diff import ColumnRename
from playhouse.schema_diff import IndexDiff
from playhouse.schema_diff import SchemaDiff
from playhouse.schema_diff import TableRename
from playhouse.schema_diff import diff_models
from playhouse.schema_diff import plan_renames

from .base import IS_CRDB
from .base import IS_MYSQL
from .base import ModelTestCase
from .base import TestModel
from .base import requires_models
from .base import requires_sqlite
from .base import skip_if


class SdUser(TestModel):
    username = CharField(unique=True)
    email = CharField(index=True)

    class Meta:
        table_name = 'sd_user'


class SdTweet(TestModel):
    user = ForeignKeyField(SdUser, backref='tweets')
    content = TextField()
    flags = IntegerField(default=0)

    class Meta:
        table_name = 'sd_tweet'
        indexes = ((('user', 'flags'), False),)


class SdPoints(TestModel):
    user = ForeignKeyField(SdUser)
    key = CharField(max_length=32)

    class Meta:
        table_name = 'sd_points'
        primary_key = CompositeKey('user', 'key')


SD_MODELS = [SdUser, SdTweet, SdPoints]


class SdUser2(TestModel):
    # vs SdUser: email dropped, karma added, unique username index dropped,
    # composite (username, karma) index added.
    username = CharField()
    karma = IntegerField(default=0)

    class Meta:
        table_name = 'sd_user'
        indexes = ((('username', 'karma'), True),)


class SdNote(TestModel):
    user = ForeignKeyField(SdUser)
    content = TextField()

    class Meta:
        table_name = 'sd_note'


class SdPartial(TestModel):
    flags = IntegerField(default=0)

    class Meta:
        table_name = 'sd_partial'

SdPartial.add_index(SdPartial.flags, where=(SdPartial.flags > SQL('0')))


class SdDesc(TestModel):
    ts = IntegerField()

    class Meta:
        table_name = 'sd_desc'

SdDesc.add_index(SdDesc.ts.desc())


class SdSlug(TestModel):
    slug = CharField(max_length=24, primary_key=True)
    alt = CharField(unique=True)

    class Meta:
        table_name = 'sd_slug'


class SdBadge(TestModel):
    owner = ForeignKeyField(SdUser, backref='badges', on_delete='CASCADE')
    slug = ForeignKeyField(SdSlug, backref='badges')
    alt = ForeignKeyField(SdSlug, field=SdSlug.alt, backref='alt_badges',
                          null=True)
    parent = ForeignKeyField('self', null=True)

    class Meta:
        table_name = 'sd_badge'


class SdAccount(TestModel):
    label = CharField()

    class Meta:
        table_name = 'sd_account'


class SdSettings(TestModel):
    # One-to-one extension: the pk is itself an fk.
    account = ForeignKeyField(SdAccount, primary_key=True)
    theme = CharField()

    class Meta:
        table_name = 'sd_settings'


class SdPerson(TestModel):
    # sd_user renamed: email -> mail, karma added.
    username = CharField(unique=True)
    mail = CharField(index=True)
    karma = IntegerField(default=0)

    class Meta:
        table_name = 'sd_person'


class SdTweetAuthor(TestModel):
    # sd_tweet with the fk column and flags column renamed; the
    # composite index declaration follows the new names.
    author = ForeignKeyField(SdUser, column_name='author_id')
    content = TextField()
    score = IntegerField(default=0)

    class Meta:
        table_name = 'sd_tweet'
        indexes = ((('author', 'score'), False),)


class SdLeft(TestModel):
    name = CharField(unique=True)

    class Meta:
        table_name = 'sd_left'


class SdRight(TestModel):
    name = CharField(unique=True)

    class Meta:
        table_name = 'sd_right'


class SdLeftAtRight(TestModel):
    # sd_left's shape, now declared under sd_right.
    name = CharField(unique=True)

    class Meta:
        table_name = 'sd_right'


class SdRightAtLeft(TestModel):
    # sd_right's shape, now declared under sd_left.
    name = CharField(unique=True)

    class Meta:
        table_name = 'sd_left'


class SdColSwap(TestModel):
    a = IntegerField()
    b = IntegerField()

    class Meta:
        table_name = 'sd_colswap'


@skip_if(IS_CRDB, 'crdb introspection differs')
class TestSchemaDiff(ModelTestCase):
    requires = SD_MODELS

    def test_clean_schema(self):
        # Includes sd_points, whose composite-pk backing index must not
        # read as a change.
        diff = diff_models(self.database, SD_MODELS)
        self.assertFalse(diff)
        for part in diff:
            self.assertEqual(part, [])

    def test_column_add_drop(self):
        diff = diff_models(self.database, [SdUser2, SdTweet, SdPoints])
        self.assertEqual([f.column_name for f in diff.add_columns],
                         ['karma'])
        self.assertTrue(diff.add_columns[0].model is SdUser2)
        self.assertEqual(diff.drop_columns, [('sd_user', 'email')])

        # The email index and unique username index are no longer
        # declared. The composite is new.
        self.assertEqual(diff.add_indexes,
                         [IndexDiff('sd_user', None, ('username', 'karma'),
                                    True)])
        self.assertEqual(diff.drop_indexes,
                         [IndexDiff('sd_user', 'sd_user_email', ('email',),
                                    False),
                          IndexDiff('sd_user', 'sd_user_username',
                                    ('username',), True)])
        self.assertEqual(diff.create_tables, [])

    def test_create_tables_dependency_order(self):
        class SdReply(TestModel):
            note = ForeignKeyField(SdNote)

            class Meta:
                table_name = 'sd_reply'

        diff = diff_models(self.database, [SdReply, SdNote] + SD_MODELS)
        # Referenced tables sort ahead of referencing ones.
        self.assertEqual(diff.create_tables, [SdNote, SdReply])
        self.assertFalse(diff.add_columns or diff.drop_columns or
                         diff.add_indexes or diff.drop_indexes)

    def test_stray_index_dropped(self):
        if IS_MYSQL:  # Mysql requires a prefix length to index TEXT.
            self.database.execute_sql(
                'CREATE INDEX sd_extra ON sd_tweet (content(16))')
        else:
            self.database.execute_sql(
                'CREATE INDEX sd_extra ON sd_tweet (content)')

        diff = diff_models(self.database, SD_MODELS)
        self.assertEqual(diff.add_indexes, [])
        self.assertEqual(diff.drop_indexes,
                         [IndexDiff('sd_tweet', 'sd_extra', ('content',),
                                    False)])

    def test_unique_flag_mismatch(self):
        # Same columns but different uniqueness: reported as add + drop.
        if IS_MYSQL:
            self.database.execute_sql(
                'DROP INDEX sd_tweet_user_id_flags ON sd_tweet')
        else:
            self.database.execute_sql('DROP INDEX sd_tweet_user_id_flags')
        self.database.execute_sql(
            'CREATE UNIQUE INDEX sd_tweet_user_id_flags ON '
            'sd_tweet (user_id, flags)')

        diff = diff_models(self.database, SD_MODELS)
        self.assertEqual(diff.add_indexes,
                         [IndexDiff('sd_tweet', None, ('user_id', 'flags'),
                                    False)])
        self.assertEqual(diff.drop_indexes,
                         [IndexDiff('sd_tweet', 'sd_tweet_user_id_flags',
                                    ('user_id', 'flags'), True)])

    @requires_models(SdDesc)
    def test_descending_index(self):
        # The database reports the ts.desc() index as plain (ts) columns.
        # Pairing it by name keeps both sides from churning.
        self.assertFalse(diff_models(self.database, [SdDesc]))

    @requires_sqlite
    @requires_models(SdPartial)
    def test_partial_indexes_by_name(self):
        # Present on both sides: matched by name.
        self.assertFalse(diff_models(self.database, [SdPartial]))

        # Missing from the database: suggested by name, with no details.
        self.database.execute_sql('DROP INDEX sd_partial_flags')
        diff = diff_models(self.database, [SdPartial])
        self.assertEqual(diff.add_indexes,
                         [IndexDiff('sd_partial', 'sd_partial_flags', None,
                                    None)])

        # In the database but not declared: suggested for removal.
        self.database.execute_sql(
            'CREATE INDEX sd_partial_flags ON sd_partial (flags) '
            'WHERE (flags > 0)')
        self.database.execute_sql(
            'CREATE INDEX sd_partial_stray ON sd_partial (flags) '
            'WHERE (flags < 0)')
        diff = diff_models(self.database, [SdPartial])
        self.assertEqual(diff.add_indexes, [])
        self.assertEqual(diff.drop_indexes,
                         [IndexDiff('sd_partial', 'sd_partial_stray', None,
                                    None)])

    def test_duplicate_table_name(self):
        # Two models on one table: the first wins, no drift reported.
        diff = diff_models(self.database, [SdUser, SdUser2])
        self.assertFalse(diff)

    def test_rename_table_and_column(self):
        # The table rename carries the email -> mail rename (keyed by the
        # old table name) and the karma addition. No create/drop, and the
        # single-column mail index pairs with the old email index.
        diff = diff_models(
            self.database, [SdPerson, SdTweet, SdPoints],
            table_renames={'sd_user': 'sd_person'},
            column_renames={'sd_user': {'email': 'mail'}})
        self.assertEqual(diff.rename_tables,
                         [TableRename(None, 'sd_user', 'sd_person')])
        self.assertEqual(diff.rename_columns,
                         [ColumnRename(None, 'sd_person', 'email', 'mail')])
        self.assertEqual([f.column_name for f in diff.add_columns],
                         ['karma'])
        self.assertFalse(diff.create_tables or diff.drop_columns or
                         diff.add_indexes or diff.drop_indexes)

    def test_rename_column_keeps_composite_index(self):
        # Both columns of the composite index move; the database index on
        # (user_id, flags) is read through the mapping and pairs, instead
        # of dropping and recreating.
        diff = diff_models(
            self.database, [SdUser, SdTweetAuthor, SdPoints],
            column_renames={'sd_tweet': {'user_id': 'author_id',
                                         'flags': 'score'}})
        self.assertEqual(diff.rename_columns,
                         [ColumnRename(None, 'sd_tweet', 'flags', 'score'),
                          ColumnRename(None, 'sd_tweet', 'user_id',
                                       'author_id')])
        self.assertFalse(diff.create_tables or diff.rename_tables or
                         diff.add_columns or diff.drop_columns or
                         diff.add_indexes or diff.drop_indexes)

    @requires_models(SdLeft, SdRight)
    def test_swap_tables(self):
        diff = diff_models(
            self.database, [SdLeftAtRight, SdRightAtLeft],
            table_renames={'sd_left': 'sd_right', 'sd_right': 'sd_left'})
        self.assertEqual(sorted(diff.rename_tables),
                         sorted([TableRename(None, 'sd_left', 'sd_right'),
                                 TableRename(None, 'sd_right', 'sd_left')]))
        self.assertFalse(diff.create_tables or diff.add_columns or
                         diff.drop_columns or diff.add_indexes or
                         diff.drop_indexes)

    @requires_models(SdColSwap)
    def test_swap_columns(self):
        diff = diff_models(
            self.database, [SdColSwap],
            column_renames={'sd_colswap': {'a': 'b', 'b': 'a'}})
        self.assertEqual(sorted(diff.rename_columns),
                         sorted([ColumnRename(None, 'sd_colswap', 'a', 'b'),
                                 ColumnRename(None, 'sd_colswap', 'b',
                                              'a')]))
        self.assertFalse(diff.create_tables or diff.rename_tables or
                         diff.add_columns or diff.drop_columns or
                         diff.add_indexes or diff.drop_indexes)

    def test_plan_renames(self):
        self.assertEqual(plan_renames([('a', 'b')], {'a'}),
                         [('a', 'b')])
        steps = plan_renames([('a', 'b'), ('b', 'a')], {'a', 'b'})
        self.assertEqual(len(steps), 3)
        self.assertEqual(steps[0][0], 'a')
        self.assertTrue(steps[0][1].startswith('__pw_rename_tmp_'))
        self.assertEqual(steps[1], ('b', 'a'))
        self.assertEqual((steps[2][0], steps[2][1]),
                         (steps[0][1], 'b'))
        # An unmoved occupant is an error, not an infinite loop.
        with self.assertRaises(ValueError):
            plan_renames([('a', 'c')], {'a', 'c'})

    def test_invalid_renames(self):
        before = self.database.get_tables()
        cases = [
            ({'sd_missing': 'sd_user'}, None),
            ({'sd_user': 'sd_missing'}, None),
            ({'sd_user': 'sd_user'}, None),
            ({'sd_user': 'sd_tweet'}, None),
            ({'sd_user': 'sd_points', 'sd_tweet': 'sd_points'}, None),
            ({'sd_user': 'a.b'}, None),
            (None, {'sd_user': {'missing': 'username'}}),
            (None, {'sd_user': {'email': 'missing'}}),
            (None, {'sd_user': {'username': 'email'}}),
            (None, {'sd_user': {'email': 'email'}}),
            (None, {'sd_missing': {'email': 'mail'}}),
        ]
        for table_renames, column_renames in cases:
            with self.subTest(table_renames=table_renames,
                              column_renames=column_renames):
                with self.assertRaises(ValueError):
                    diff_models(self.database, SD_MODELS,
                                table_renames=table_renames,
                                column_renames=column_renames)
        # Validation is read-only: nothing was modified.
        self.assertEqual(before, self.database.get_tables())

    @requires_sqlite
    def test_qualified_renames_rejected_on_sqlite(self):
        with self.assertRaises(ValueError):
            diff_models(self.database, [SdPerson, SdTweet, SdPoints],
                        table_renames={'main.sd_user': 'sd_person'})
        with self.assertRaises(ValueError):
            diff_models(self.database, SD_MODELS,
                        column_renames={'main.sd_user': {'email': 'mail'}})

    def test_rename_display(self):
        diff = diff_models(
            self.database, [SdPerson, SdTweet, SdPoints],
            table_renames={'sd_user': 'sd_person'},
            column_renames={'sd_user': {'email': 'mail'}})
        text = str(diff)
        self.assertIn('rename table sd_user to sd_person', text)
        self.assertIn('rename column sd_person.email to mail', text)

    def test_display(self):
        diff = diff_models(self.database, [SdUser2, SdNote, SdTweet,
                                           SdPoints])
        self.assertEqual(str(diff), (
            'create table sd_note\n'
            'add column sd_user.karma\n'
            'drop column sd_user.email\n'
            'add index sd_user (username, karma) unique\n'
            'drop index sd_user.sd_user_email (email)\n'
            'drop index sd_user.sd_user_username (username) unique'))

    @requires_sqlite
    def test_virtual_models_skipped(self):
        try:
            from playhouse.sqlite_ext import FTS5Model, SearchField
        except ImportError:
            return self.skipTest('sqlite_ext unavailable')

        class SdIdx(FTS5Model):
            content = SearchField()

            class Meta:
                database = self.database

        if not SdIdx.fts5_installed():
            return self.skipTest('fts5 unavailable')
        self.database.create_tables([SdIdx])
        try:
            self.assertFalse(diff_models(self.database, [SdIdx] + SD_MODELS))
        finally:
            self.database.drop_tables([SdIdx])


def strip_header(body):
    # Drop the timestamped '# Generated ...' comment line.
    return body.split('\n', 1)[1]


# Everything at once: new table (fk against a frozen stub), column
# add/drop, index changes, TODOs for the unrestorable. down() holds the
# certain inverses only, walked in reverse.
FULL_BODY = """\
from peewee import *

# TODO: sd_user: dropped index sd_user_email cannot be restored by down() (column dropped)
# TODO: sd_user.email: dropped column cannot be restored by down()

def up(migrator, db):
    class SdUser(Model):
        class Meta:
            database = db
            table_name = 'sd_user'

    class SdNote(Model):
        user = ForeignKeyField(SdUser)
        content = TextField()
        class Meta:
            database = db
            table_name = 'sd_note'
    db.create_tables([SdNote])

    migrator.migrate(migrator.drop_index('sd_user', 'sd_user_email'))
    migrator.migrate(migrator.drop_index('sd_user', 'sd_user_username'))
    migrator.migrate(migrator.add_column('sd_user', 'karma', IntegerField(default=0)))
    migrator.migrate(migrator.add_index('sd_user', ('username', 'karma'), unique=True))
    migrator.migrate(migrator.drop_column('sd_user', 'email'))


def down(migrator, db):
    migrator.migrate(migrator.drop_index('sd_user', 'sd_user_username_karma'))
    migrator.migrate(migrator.drop_column('sd_user', 'karma'))
    migrator.migrate(migrator.add_index('sd_user', ('username',), unique=True))
    migrator.migrate(migrator.drop_table('sd_note'))
"""

# A non-default fk column name survives onto the rendered fk.
FK_ALIAS_BODY = """\
from peewee import *

def up(migrator, db):
    class SdUser(Model):
        class Meta:
            database = db
            table_name = 'sd_user'

    class SdOwned(Model):
        owner = ForeignKeyField(SdUser, column_name='owner')
        label = CharField()
        class Meta:
            database = db
            table_name = 'sd_owned'
    db.create_tables([SdOwned])


def down(migrator, db):
    migrator.migrate(migrator.drop_table('sd_owned'))
"""

# Added fks render runnable against stubs, with the target field written
# out (add_column fields never get bound). index=True, the fk default,
# stays out: add_column() indexes the column itself. down() still drops
# the indexes before the columns. The not-null add gets a TODO, since
# add_column() refuses it without a default.
ADDED_FK_BODY = """\
from peewee import *

# TODO: sd_tweet.owner_id: not-null column needs a default or allow_not_null backfill

def up(migrator, db):
    class SdTweetFks(Model):
        class Meta:
            database = db
            table_name = 'sd_tweet'

    class SdUser(Model):
        class Meta:
            database = db
            table_name = 'sd_user'

    migrator.migrate(migrator.add_column('sd_tweet', 'editor_id', ForeignKeyField(SdUser, field=SdUser.id, unique=True, null=True)))
    migrator.migrate(migrator.add_column('sd_tweet', 'parent_id', ForeignKeyField(SdTweetFks, field=SdTweetFks.id, null=True)))
    migrator.migrate(migrator.add_column('sd_tweet', 'owner_id', ForeignKeyField(SdUser, field=SdUser.id)))


def down(migrator, db):
    migrator.migrate(migrator.drop_index('sd_tweet', 'sd_tweet_parent_id'))
    migrator.migrate(migrator.drop_index('sd_tweet', 'sd_tweet_owner_id'))
    migrator.migrate(migrator.drop_index('sd_tweet', 'sd_tweet_editor_id'))
    migrator.migrate(migrator.drop_column('sd_tweet', 'owner_id'))
    migrator.migrate(migrator.drop_column('sd_tweet', 'parent_id'))
    migrator.migrate(migrator.drop_column('sd_tweet', 'editor_id'))
"""

# Fk kwargs: on_delete is preserved, a non-pk target renders field=, a
# target created in the same diff is referenced directly (dependency
# order), and a self-reference stays 'self'.
FK_KWARGS_BODY = """\
from peewee import *

def up(migrator, db):
    class SdUser(Model):
        class Meta:
            database = db
            table_name = 'sd_user'

    class SdSlug(Model):
        slug = CharField(primary_key=True, max_length=24)
        alt = CharField(unique=True)
        class Meta:
            database = db
            table_name = 'sd_slug'
    db.create_tables([SdSlug])

    class SdBadge(Model):
        owner = ForeignKeyField(SdUser, on_delete='CASCADE')
        slug = ForeignKeyField(SdSlug)
        alt = ForeignKeyField(SdSlug, field=SdSlug.alt, null=True)
        parent = ForeignKeyField('self', null=True)
        class Meta:
            database = db
            table_name = 'sd_badge'
    db.create_tables([SdBadge])


def down(migrator, db):
    migrator.migrate(migrator.drop_table('sd_badge'))
    migrator.migrate(migrator.drop_table('sd_slug'))
"""

# A partial index is known by name only, so up() stays empty.
PARTIAL_TODO_BODY = """\
from peewee import *

# TODO: sd_partial: create index sd_partial_flags (partial/expression, details not detected)

def up(migrator, db):
    pass


def down(migrator, db):
    pass
"""

DROP_RESTORE_NAME_BODY = """\
from peewee import *

def up(migrator, db):
    migrator.migrate(migrator.drop_index('sd_tweet', 'sd_extra'))


def down(migrator, db):
    migrator.migrate(migrator.add_index('sd_tweet', ('content',), name='sd_extra'))
"""

SCHEMA_BODY = """\
from peewee import *

def up(migrator, db):
    class SdAux(Model):
        label = CharField()
        class Meta:
            database = db
            table_name = 'sd_aux'
            schema = 'aux'
    db.create_tables([SdAux])


def down(migrator, db):
    migrator.migrate(migrator.drop_table('sd_aux', schema='aux'))
"""

FIELD_PARAMS_BODY = """\
from peewee import *

def up(migrator, db):
    class SdSku(Model):
        code = CharField(unique=True, max_length=32)
        price = DecimalField(max_digits=12, decimal_places=2)
        label = CharField()
        class Meta:
            database = db
            table_name = 'sd_sku'
    db.create_tables([SdSku])


def down(migrator, db):
    migrator.migrate(migrator.drop_table('sd_sku'))
"""

DEFAULTS_BODY = """\
from peewee import *

# TODO: uval: default {} must be added by hand

def up(migrator, db):
    class SdFieldDefaults(Model):
        ival = IntegerField(default=0)
        sval = CharField(max_length=10, default='')
        bval = BooleanField(default=True)
        uval = JSONField()
        class Meta:
            database = db
            table_name = 'sd_field_defaults'
    db.create_tables([SdFieldDefaults])


def down(migrator, db):
    migrator.migrate(migrator.drop_table('sd_field_defaults'))
"""

# Recognized common defaults render with their imports. IntEnum stores
# its plain value.
COMMON_DEFAULTS_BODY = """\
from peewee import *
import datetime
import decimal
import time
import uuid

def up(migrator, db):
    class SdCommonDefaults(Model):
        created = DateTimeField(default=datetime.datetime.now)
        updated = DateTimeField(default=datetime.datetime.utcnow)
        day = DateField(default=datetime.date.today)
        stamp = FloatField(default=time.time)
        stamp_ns = IntegerField(default=time.time_ns)
        token = UUIDField(default=uuid.uuid4)
        meta = JSONField(default=dict)
        tags = JSONField(default=list)
        amount = DecimalField(default=decimal.Decimal('0.01'))
        color = IntegerField(default=1)
        class Meta:
            database = db
            table_name = 'sd_common_defaults'
    db.create_tables([SdCommonDefaults])


def down(migrator, db):
    migrator.migrate(migrator.drop_table('sd_common_defaults'))
"""

# A field class from outside core peewee brings its own import.
IMPORT_BODY = """\
from peewee import *
from playhouse.sqlite_ext import JSONField

def up(migrator, db):
    class SdExt(Model):
        data = JSONField()
        class Meta:
            database = db
            table_name = 'sd_ext'
    db.create_tables([SdExt])


def down(migrator, db):
    migrator.migrate(migrator.drop_table('sd_ext'))
"""

# Enum fields render as their storage type, free of the enum class.
ENUM_BODY = """\
from peewee import *

def up(migrator, db):
    class SdEnum(Model):
        color = CharField(default='dark')
        level = IntegerField()
        class Meta:
            database = db
            table_name = 'sd_enum'
    db.create_tables([SdEnum])


def down(migrator, db):
    migrator.migrate(migrator.drop_table('sd_enum'))
"""

ADDED_INDEXED_COLUMN_BODY = """\
from peewee import *

def up(migrator, db):
    migrator.migrate(migrator.add_column('sd_user', 'karma', IntegerField(index=True, default=0)))


def down(migrator, db):
    migrator.migrate(migrator.drop_index('sd_user', 'sd_user_karma'))
    migrator.migrate(migrator.drop_column('sd_user', 'karma'))
"""

FK_STUB_PK_FK_BODY = """\
from peewee import *

def up(migrator, db):
    class SdSettings(Model):
        account = IntegerField(primary_key=True, column_name='account_id')
        class Meta:
            database = db
            table_name = 'sd_settings'

    class SdTheme(Model):
        settings = ForeignKeyField(SdSettings)
        name = CharField()
        class Meta:
            database = db
            table_name = 'sd_theme'
    db.create_tables([SdTheme])


def down(migrator, db):
    migrator.migrate(migrator.drop_table('sd_theme'))
"""

# A renamed table renders RENAME (data, fks, indexes move with it); a
# column renamed on the renamed table addresses it by its new name, and
# an added column rides along afterwards. down() reverses each step.
RENAME_TABLE_BODY = """\
from peewee import *

def up(migrator, db):
    migrator.migrate(migrator.rename_table('sd_user', 'sd_person'))
    migrator.migrate(migrator.rename_column('sd_person', 'email', 'mail'))
    migrator.migrate(migrator.add_column('sd_person', 'karma', IntegerField(default=0)))


def down(migrator, db):
    migrator.migrate(migrator.drop_column('sd_person', 'karma'))
    migrator.migrate(migrator.rename_column('sd_person', 'mail', 'email'))
    migrator.migrate(migrator.rename_table('sd_person', 'sd_user'))
"""

RENAME_COLUMN_BODY = """\
from peewee import *

def up(migrator, db):
    migrator.migrate(migrator.rename_column('sd_tweet', 'flags', 'score'))
    migrator.migrate(migrator.rename_column('sd_tweet', 'user_id', 'author_id'))


def down(migrator, db):
    migrator.migrate(migrator.rename_column('sd_tweet', 'author_id', 'user_id'))
    migrator.migrate(migrator.rename_column('sd_tweet', 'score', 'flags'))
"""

# Swapping two table names goes through a temporary name; down() is the
# exact inverse, walked backwards.
SWAP_TABLE_BODY = """\
from peewee import *

def up(migrator, db):
    migrator.migrate(migrator.rename_table('sd_left', '__pw_rename_tmp_0'))
    migrator.migrate(migrator.rename_table('sd_right', 'sd_left'))
    migrator.migrate(migrator.rename_table('__pw_rename_tmp_0', 'sd_right'))


def down(migrator, db):
    migrator.migrate(migrator.rename_table('sd_right', '__pw_rename_tmp_0'))
    migrator.migrate(migrator.rename_table('sd_left', 'sd_right'))
    migrator.migrate(migrator.rename_table('__pw_rename_tmp_0', 'sd_left'))
"""

@skip_if(IS_CRDB, 'crdb introspection differs')
class TestTemplate(ModelTestCase):
    requires = SD_MODELS

    def test_full_body(self):
        body = template(diff_models(self.database,
                                    [SdUser2, SdNote, SdTweet, SdPoints]))
        self.assertEqual(strip_header(body), FULL_BODY)

    def test_rename_table_body(self):
        diff = diff_models(
            self.database, [SdPerson, SdTweet, SdPoints],
            table_renames={'sd_user': 'sd_person'},
            column_renames={'sd_user': {'email': 'mail'}})
        self.assertEqual(strip_header(template(diff)), RENAME_TABLE_BODY)

    def test_rename_column_body(self):
        diff = diff_models(
            self.database, [SdUser, SdTweetAuthor, SdPoints],
            column_renames={'sd_tweet': {'user_id': 'author_id',
                                         'flags': 'score'}})
        self.assertEqual(strip_header(template(diff)), RENAME_COLUMN_BODY)

    @requires_models(SdLeft, SdRight)
    def test_swap_table_body(self):
        diff = diff_models(
            self.database, [SdLeftAtRight, SdRightAtLeft],
            table_renames={'sd_left': 'sd_right', 'sd_right': 'sd_left'})
        self.assertEqual(strip_header(template(diff)), SWAP_TABLE_BODY)

    def test_schema_qualified_rename_body(self):
        # Schema is rendered explicitly, like drop_table, so the file is
        # runnable against a backend migrator bound to the schema.
        diff = SchemaDiff(
            [], [TableRename('aux', 'sd_wheel', 'sd_tyre')],
            [ColumnRename('aux', 'sd_tyre', 'brand', 'make')],
            [], [], [], [])
        body = strip_header(template(diff))
        self.assertIn(
            "migrator.rename_table('sd_wheel', 'sd_tyre', schema='aux')",
            body)
        self.assertIn(
            "migrator.rename_column('sd_tyre', 'brand', 'make', "
            "schema='aux')",
            body)
        self.assertIn(
            "migrator.rename_table('sd_tyre', 'sd_wheel', schema='aux')",
            body)

    def test_fk_alias(self):
        class SdOwned(TestModel):
            owner = ForeignKeyField(SdUser, column_name='owner')
            label = CharField()

            class Meta:
                table_name = 'sd_owned'

        body = template(diff_models(self.database, [SdOwned] + SD_MODELS))
        self.assertEqual(strip_header(body), FK_ALIAS_BODY)

    def test_added_fk_flags(self):
        class SdTweetFks(TestModel):
            user = ForeignKeyField(SdUser, backref='tweets')
            content = TextField()
            flags = IntegerField(default=0)
            editor = ForeignKeyField(SdUser, backref='edits', unique=True,
                                     null=True)
            parent = ForeignKeyField('self', null=True)
            owner = ForeignKeyField(SdUser, backref='owned')

            class Meta:
                table_name = 'sd_tweet'
                indexes = ((('user', 'flags'), False),)

        body = template(diff_models(self.database,
                                    [SdTweetFks, SdUser, SdPoints]))
        self.assertEqual(strip_header(body), ADDED_FK_BODY)

    def test_fk_kwargs(self):
        body = template(diff_models(self.database,
                                    [SdBadge, SdSlug] + SD_MODELS))
        self.assertEqual(strip_header(body), FK_KWARGS_BODY)

    @requires_sqlite
    @requires_models(SdPartial)
    def test_partial_index_todo(self):
        self.database.execute_sql('DROP INDEX sd_partial_flags')
        body = template(diff_models(self.database, [SdPartial]))
        self.assertEqual(strip_header(body), PARTIAL_TODO_BODY)

    def test_dropped_index_restored_by_name(self):
        if IS_MYSQL:  # Mysql requires a prefix length to index TEXT.
            self.database.execute_sql(
                'CREATE INDEX sd_extra ON sd_tweet (content(16))')
        else:
            self.database.execute_sql(
                'CREATE INDEX sd_extra ON sd_tweet (content)')

        body = template(diff_models(self.database, SD_MODELS))
        self.assertEqual(strip_header(body), DROP_RESTORE_NAME_BODY)

    @requires_sqlite
    def test_schema_rendered(self):
        self.database.execute_sql("ATTACH ':memory:' AS aux")

        class SdAux(TestModel):
            label = CharField()

            class Meta:
                table_name = 'sd_aux'
                schema = 'aux'

        body = template(diff_models(self.database, [SdAux]))
        self.assertEqual(strip_header(body), SCHEMA_BODY)

    def test_field_defaults(self):
        class SdFieldDefaults(TestModel):
            ival = IntegerField(default=0)
            sval = CharField(max_length=10, default='')
            bval = BooleanField(default=True)
            uval = JSONField(default={})

        body = template(diff_models(self.database, [SdFieldDefaults]))
        self.assertEqual(strip_header(body), DEFAULTS_BODY)

    def test_common_defaults(self):
        class SdColor(enum.IntEnum):
            RED = 1

        class SdCommonDefaults(TestModel):
            created = DateTimeField(default=datetime.datetime.now)
            updated = DateTimeField(default=datetime.datetime.utcnow)
            day = DateField(default=datetime.date.today)
            stamp = FloatField(default=time.time)
            stamp_ns = IntegerField(default=time.time_ns)
            token = UUIDField(default=uuid.uuid4)
            meta = JSONField(default=dict)
            tags = JSONField(default=list)
            amount = DecimalField(default=decimal.Decimal('0.01'))
            color = IntegerField(default=SdColor.RED)

        body = template(diff_models(self.database, [SdCommonDefaults]))
        self.assertEqual(strip_header(body), COMMON_DEFAULTS_BODY)

    def test_field_import(self):
        # Import locally: module-level it would shadow core JSONField.
        from playhouse.sqlite_ext import JSONField

        class SdExt(TestModel):
            data = JSONField()

        body = template(diff_models(self.database, [SdExt]))
        self.assertEqual(strip_header(body), IMPORT_BODY)

    def test_enum_field(self):
        class SdColor(enum.IntEnum):
            RED = 1

        class SdShade(enum.Enum):
            DARK = 'dark'

        class SdEnum(TestModel):
            color = EnumField(SdShade, default=SdShade.DARK)
            level = IntEnumField(SdColor)

        body = template(diff_models(self.database, [SdEnum]))
        self.assertEqual(strip_header(body), ENUM_BODY)


@skip_if(IS_CRDB, 'crdb introspection differs')
class TestTemplateRoundTrip(ModelTestCase):
    requires = SD_MODELS

    def setUp(self):
        super(TestTemplateRoundTrip, self).setUp()
        self.dir = tempfile.mkdtemp()
        self.runner = Runner(self.database, self.dir)

    def tearDown(self):
        try:
            shutil.rmtree(self.dir, ignore_errors=True)
            self.database.drop_tables([self.runner.History], safe=True)
            for table in ('sd_theme', 'sd_settings', 'sd_account',
                          'sd_badge', 'sd_slug', 'sd_tag', 'sd_sku',
                          'sd_profile', 'sd_alias', 'sd_kv', 'sd_person',
                          'sd_left', 'sd_right', 'sd_colswap'):
                self.database.execute_sql('DROP TABLE IF EXISTS %s' % table)
        finally:
            super(TestTemplateRoundTrip, self).tearDown()
            self.database.close()

    def apply(self, body, name):
        self.runner.create(name, body=body)
        self.assertEqual(self.runner.up(), ['0001_%s' % name])

    def test_round_trip(self):
        # Fully runnable as generated: new table, index changes, column
        # drop.
        class SdUser3(TestModel):
            username = CharField(unique=True)

            class Meta:
                table_name = 'sd_user'

        class SdTag(TestModel):
            label = CharField(unique=True)
            position = IntegerField(index=True)

            class Meta:
                table_name = 'sd_tag'
                indexes = ((('label', 'position'), False),)

        models = [SdUser3, SdTag, SdTweet, SdPoints]
        body = template(diff_models(self.database, models))
        self.apply(body, 'auto')
        self.assertFalse(diff_models(self.database, models))

        # down() reverts the certain subset: the dropped email column and
        # its index stay gone, everything else returns.
        self.assertEqual(self.runner.down(), ['0001_auto'])
        diff = diff_models(self.database, models)
        self.assertEqual([m.__name__ for m in diff.create_tables],
                         ['SdTag'])
        self.assertFalse(diff.add_columns or diff.drop_columns or
                         diff.drop_indexes)

    def test_field_params(self):
        # Ddl-affecting values (length, precision) are rendered and class
        # defaults are omitted.
        class SdSku(TestModel):
            code = CharField(max_length=32, unique=True)
            price = DecimalField(max_digits=12, decimal_places=2)
            label = CharField()

            class Meta:
                table_name = 'sd_sku'

        body = template(diff_models(self.database, [SdSku] + SD_MODELS))
        self.assertEqual(strip_header(body), FIELD_PARAMS_BODY)

        self.apply(body, 'sku')
        self.assertFalse(diff_models(self.database, [SdSku] + SD_MODELS))

    def test_unique_flip(self):
        # unique=True -> index=True re-uses the index name. The drop must
        # precede the add or every backend errors.
        class SdUserFlip(TestModel):
            username = CharField(index=True)
            email = CharField(index=True)

            class Meta:
                table_name = 'sd_user'

        models = [SdUserFlip, SdTweet, SdPoints]
        body = template(diff_models(self.database, models))
        self.apply(body, 'flip')
        self.assertFalse(diff_models(self.database, models))
        # And the reverse flip on the way down.
        self.assertEqual(self.runner.down(), ['0001_flip'])
        self.assertFalse(diff_models(self.database, SD_MODELS))

    def test_added_indexed_column(self):
        # add_column() creates the field's index itself, so up() needs no
        # separate add_index. down() must still drop the index before the
        # column or sqlite refuses the drop.
        class SdUserKarma(TestModel):
            username = CharField(unique=True)
            email = CharField(index=True)
            karma = IntegerField(index=True, default=0)

            class Meta:
                table_name = 'sd_user'

        models = [SdUserKarma, SdTweet, SdPoints]
        body = template(diff_models(self.database, models))
        self.assertEqual(strip_header(body), ADDED_INDEXED_COLUMN_BODY)

        self.apply(body, 'karma')
        self.assertFalse(diff_models(self.database, models))
        self.assertEqual(self.runner.down(), ['0001_karma'])
        self.assertFalse(diff_models(self.database, SD_MODELS))

    def test_fk_flags(self):
        # unique/null must render on the fks or the created table never
        # converges (unique) or silently comes out NOT NULL (null).
        class SdProfile(TestModel):
            user = ForeignKeyField(SdUser, backref='profiles', unique=True)
            editor = ForeignKeyField(SdUser, backref='edited', null=True)

            class Meta:
                table_name = 'sd_profile'

        models = [SdProfile] + SD_MODELS
        body = template(diff_models(self.database, models))
        self.apply(body, 'profile')
        self.assertFalse(diff_models(self.database, models))
        columns = {c.name: c
                   for c in self.database.get_columns('sd_profile')}
        self.assertTrue(columns['editor_id'].null)
        self.assertFalse(columns['user_id'].null)
        self.assertEqual(self.runner.down(), ['0001_profile'])

    def test_fk_chain(self):
        # Two new tables, one referencing the other plus stubbed and
        # self targets. The generated file runs and reverts as-is.
        models = [SdBadge, SdSlug] + SD_MODELS
        body = template(diff_models(self.database, models))
        self.apply(body, 'chain')
        self.assertFalse(diff_models(self.database, models))

        self.assertEqual(self.runner.down(), ['0001_chain'])
        diff = diff_models(self.database, models)
        self.assertEqual(diff.create_tables, [SdSlug, SdBadge])

    @requires_models(SdSlug)
    def test_fk_stub(self):
        # sd_slug exists, so its typed pk and referenced unique column
        # must come through on the stub or creating sd_badge fails.
        models = [SdBadge, SdSlug] + SD_MODELS
        body = template(diff_models(self.database, models))
        self.apply(body, 'badge')
        self.assertFalse(diff_models(self.database, models))

        self.assertEqual(self.runner.down(), ['0001_badge'])
        diff = diff_models(self.database, models)
        self.assertEqual(diff.create_tables, [SdBadge])

    @requires_models(SdAccount, SdSettings)
    def test_fk_stub_pk_fk(self):
        # sd_settings' pk is itself an fk. The stub renders it as a
        # plain integer rather than recursing into sd_account.
        class SdTheme(TestModel):
            settings = ForeignKeyField(SdSettings)
            name = CharField()

            class Meta:
                table_name = 'sd_theme'

        models = [SdTheme, SdSettings, SdAccount] + SD_MODELS
        body = template(diff_models(self.database, models))
        self.assertEqual(strip_header(body), FK_STUB_PK_FK_BODY)
        self.apply(body, 'theme')
        self.assertFalse(diff_models(self.database, models))

        self.assertEqual(self.runner.down(), ['0001_theme'])
        self.assertEqual(diff_models(self.database, models).create_tables,
                         [SdTheme])

    def test_autofield_alias(self):
        # The aliased pk must render or the table never converges.
        class SdAlias(TestModel):
            id = AutoField(column_name='object_id')
            label = CharField()

            class Meta:
                table_name = 'sd_alias'

        models = [SdAlias] + SD_MODELS
        body = template(diff_models(self.database, models))
        self.apply(body, 'alias')
        self.assertFalse(diff_models(self.database, models))

    def test_non_auto_pk(self):
        # primary_key=True must render exactly once or the generated file
        # is a SyntaxError (keyword argument repeated).
        class SdKV(TestModel):
            key = CharField(max_length=32, primary_key=True)
            value = TextField()

            class Meta:
                table_name = 'sd_kv'

        models = [SdKV] + SD_MODELS
        body = template(diff_models(self.database, models))
        self.apply(body, 'kv')
        self.assertFalse(diff_models(self.database, models))

    def _fk_destinations(self, table):
        return {fk.column: fk.dest_table
                for fk in self.database.get_foreign_keys(table)}

    def _index_columns(self, table):
        return {idx.name: tuple(idx.columns)
                for idx in self.database.get_indexes(table)}

    def test_rename_table_round_trip(self):
        # A referenced parent table is renamed with its data; the child
        # fk follows, the composite index is untouched and down()
        # restores everything.
        SdUser.create(username='u1', email='a@example.com')
        tweet = SdTweet.create(user=1, content='hi', flags=7)
        SdPoints.create(user=1, key='k')

        models = [SdPerson, SdTweet, SdPoints]
        body = template(diff_models(
            self.database, models,
            table_renames={'sd_user': 'sd_person'},
            column_renames={'sd_user': {'email': 'mail'}}))
        self.apply(body, 'rename_table')

        self.assertNotIn('sd_user', self.database.get_tables())
        self.assertIn('sd_person', self.database.get_tables())

        # Data and the renamed column value survived.
        cursor = self.database.execute_sql(
            'SELECT username, mail, karma FROM sd_person')
        self.assertEqual(cursor.fetchall(),
                         [('u1', 'a@example.com', 0)])

        # Foreign keys in both children now reference sd_person.
        self.assertEqual(self._fk_destinations('sd_tweet')['user_id'],
                         'sd_person')
        self.assertEqual(self._fk_destinations('sd_points')['user_id'],
                         'sd_person')

        # The composite index is intact and usable.
        self.assertEqual(
            self._index_columns('sd_tweet')['sd_tweet_user_id_flags'],
            ('user_id', 'flags'))
        joined = self.database.execute_sql(
            'SELECT t.content, p.username FROM sd_tweet AS t '
            'JOIN sd_person AS p ON t.user_id = p.id')
        self.assertEqual(joined.fetchall(), [('hi', 'u1')])

        self.assertFalse(diff_models(self.database, models))

        self.assertEqual(self.runner.down(), ['0001_rename_table'])
        self.assertIn('sd_user', self.database.get_tables())
        self.assertNotIn('sd_person', self.database.get_tables())
        self.assertEqual(self._fk_destinations('sd_tweet')['user_id'],
                         'sd_user')
        cursor = self.database.execute_sql(
            'SELECT email FROM sd_user WHERE id = 1')
        self.assertEqual(cursor.fetchall(), [('a@example.com',)])
        self.assertEqual(SdTweet.get_by_id(tweet.id).flags, 7)
        self.assertFalse(diff_models(self.database, SD_MODELS))

    def test_rename_column_round_trip(self):
        # Renaming an fk column and a plain column keeps the rows, the fk
        # and the composite index (now under the new column names).
        SdUser.create(username='u1', email='a@example.com')
        SdTweet.create(user=1, content='hi', flags=7)

        models = [SdUser, SdTweetAuthor, SdPoints]
        body = template(diff_models(
            self.database, models,
            column_renames={'sd_tweet': {'user_id': 'author_id',
                                         'flags': 'score'}}))
        self.apply(body, 'rename_column')

        columns = {c.name for c in
                   self.database.get_columns('sd_tweet')}
        self.assertIn('author_id', columns)
        self.assertIn('score', columns)
        self.assertNotIn('user_id', columns)
        self.assertNotIn('flags', columns)

        cursor = self.database.execute_sql(
            'SELECT author_id, content, score FROM sd_tweet')
        self.assertEqual(cursor.fetchall(), [(1, 'hi', 7)])
        self.assertEqual(self._fk_destinations('sd_tweet')['author_id'],
                         'sd_user')
        self.assertEqual(
            self._index_columns('sd_tweet')['sd_tweet_user_id_flags'],
            ('author_id', 'score'))
        self.assertFalse(diff_models(self.database, models))

        self.assertEqual(self.runner.down(), ['0001_rename_column'])
        columns = {c.name for c in
                   self.database.get_columns('sd_tweet')}
        self.assertEqual({'id', 'user_id', 'content', 'flags'}, columns)
        self.assertEqual(self._fk_destinations('sd_tweet')['user_id'],
                         'sd_user')
        self.assertEqual(
            self._index_columns('sd_tweet')['sd_tweet_user_id_flags'],
            ('user_id', 'flags'))
        cursor = self.database.execute_sql(
            'SELECT user_id, flags FROM sd_tweet')
        self.assertEqual(cursor.fetchall(), [(1, 7)])
        self.assertFalse(diff_models(self.database, SD_MODELS))

    @requires_models(SdLeft, SdRight)
    def test_swap_table_round_trip(self):
        SdLeft.create(name='L')
        SdRight.create(name='R')

        models = [SdLeftAtRight, SdRightAtLeft]
        body = template(diff_models(
            self.database, models,
            table_renames={'sd_left': 'sd_right',
                           'sd_right': 'sd_left'}))
        self.apply(body, 'swap_table')

        cursor = self.database.execute_sql('SELECT name FROM sd_left')
        self.assertEqual(cursor.fetchall(), [('R',)])
        cursor = self.database.execute_sql('SELECT name FROM sd_right')
        self.assertEqual(cursor.fetchall(), [('L',)])
        self.assertFalse(diff_models(self.database, models))

        self.assertEqual(self.runner.down(), ['0001_swap_table'])
        cursor = self.database.execute_sql('SELECT name FROM sd_left')
        self.assertEqual(cursor.fetchall(), [('L',)])
        cursor = self.database.execute_sql('SELECT name FROM sd_right')
        self.assertEqual(cursor.fetchall(), [('R',)])
        self.assertFalse(diff_models(
            self.database, [SdLeft, SdRight]))

    @requires_models(SdColSwap)
    def test_swap_column_round_trip(self):
        SdColSwap.create(a=1, b=2)

        renames = {'sd_colswap': {'a': 'b', 'b': 'a'}}
        body = template(diff_models(
            self.database, [SdColSwap], column_renames=renames))
        self.apply(body, 'swap_column')

        cursor = self.database.execute_sql('SELECT a, b FROM sd_colswap')
        self.assertEqual(cursor.fetchall(), [(2, 1)])
        # The name set is unchanged by a swap ({a, b} before and after),
        # so the model already matches by name; the swap itself is an
        # explicit, data-moving operation that cannot be auto-detected as
        # "already applied" (re-supplying the mapping would plan it
        # again). Convergence is therefore read without the mapping.
        self.assertFalse(diff_models(self.database, [SdColSwap]))

        self.assertEqual(self.runner.down(), ['0001_swap_column'])
        cursor = self.database.execute_sql('SELECT a, b FROM sd_colswap')
        self.assertEqual(cursor.fetchall(), [(1, 2)])
        self.assertFalse(diff_models(self.database, [SdColSwap]))
