from __future__ import annotations

from collections.abc import Callable, Generator
from typing_extensions import assert_type

from peewee import (
    SQL,
    AutoField,
    BigBitField,
    BigBitFieldData,
    CharField,
    Check,
    DatabaseProxy,
    ForeignKeyField,
    IntegerField,
    Merge,
    Model,
    ModelAlias,
    ModelDelete,
    ModelMerge,
    ModelSelect,
    NodeList,
    NoopModelSelect,
    SqliteDatabase,
    ValuesList,
    chunked,
)


class User(Model):
    id = AutoField()
    username = CharField()
    age = IntegerField()
    nickname = CharField(null=True)


class Tweet(Model):
    user = ForeignKeyField(User)
    author = ForeignKeyField(User, null=True)


class Event(Model):
    flags = BigBitField()


# A field is a descriptor that resolves differently depending on whether it is
# accessed on the model class or on an instance. `Model.field` is the Field
# object itself (used to build queries), while `instance.field` is the stored
# Python value.
assert_type(User.username, CharField[str])
assert_type(User().username, str)

assert_type(User.age, IntegerField[int])
assert_type(User().age, int)

# `null=True` allows the value to include None, both in the field's own
# parameterization and in the value produced on attribute access.
assert_type(User.nickname, CharField[str | None])
assert_type(User().nickname, str | None)

# Foreign keys resolve to the related model instance, or None when nullable.
assert_type(Tweet.user, ForeignKeyField[User])
assert_type(Tweet().user, User)
assert_type(Tweet().author, User | None)

# The BigBitField instance descriptor yields a BigBitFieldData wrapper rather
# than the underlying bytes.
assert_type(Event.flags, BigBitField)
assert_type(Event().flags, BigBitFieldData)

# __set__ accepts the field's value type and rejects incompatible values.
user = User()
user.username = 'huey'
user.age = 42
user.nickname = None  # nullable field accepts None

user.age = 'not an int'  # type: ignore
user.username = None  # type: ignore  # non-null field rejects None


# select()/alias()/noop() are generic over the model, so the concrete subclass
# flows through to iteration and get() instead of decaying to base Model.
assert_type(User.select(), ModelSelect[User])
assert_type(User.alias(), ModelAlias[User])
assert_type(User.alias().select(), ModelSelect[User])
assert_type(User.noop(), NoopModelSelect[User])

for u in User.select():
    assert_type(u, User)
for u in User.alias().select():
    assert_type(u, User)

assert_type(User.select().get(), User)
assert_type(User.select().get_or_none(), User | None)

# delete() constructs a DELETE query and resolves only on the class. On an
# instance it raises TypeError at runtime (delete_instance() is the instance
# path), and the descriptor makes that a type error as well.
assert_type(User.delete(), ModelDelete)
User().delete()  # type: ignore


# A `database=` argument accepts a real Database or a DatabaseProxy stand-in.
proxy = DatabaseProxy()
real_db = SqliteDatabase(':memory:')
SqliteDatabase(None)  # deferred init accepts None
real_db.init(None)  # ...as does init()

User.bind(proxy)
User.bind(real_db)
User.select().execute(proxy)
User.select().execute(real_db)
User.select().get(proxy)

User.select().execute('not a database')  # type: ignore


# bind_ctx() is usable as a context-manager or as a decorator.
with User.bind_ctx(real_db):
    pass

@User.bind_ctx(real_db)
@real_db.bind_ctx([User])
def bound() -> None: ...


# after_commit() returns the callable it registers, so decorator use
# keeps the type.
def enqueue() -> int:
    return 1

assert_type(real_db.after_commit(enqueue), Callable[[], int])


# chunked() preserves the element type of the source iterable.
assert_type(chunked([1, 2, 3], 2), Generator[list[int], None, None])

# Check() returns an SQL node, or a NodeList when a constraint name is given.
assert_type(Check('age > 0'), SQL | NodeList)


# merge() accepts a ValuesList built with Merge.values() (or a Select query)
# and returns a ModelMerge; the WHEN MATCHED / NOT MATCHED builders chain and
# stay typed as ModelMerge.
merge_source = Merge.values(
    [(1, 'huey', 3)], [User.id, User.username, User.age])
assert_type(merge_source, ValuesList)
merge_query = (User
               .merge(merge_source, on=(User.id == merge_source.c.id))
               .when_matched(
                   update={User.username: merge_source.c.username},
                   condition=(merge_source.c.age > 0))
               .when_matched(delete=True)
               .when_not_matched())
assert_type(merge_query, ModelMerge)
assert_type(
    User.merge(merge_source, on=(User.id == merge_source.c.id))
    .when_not_matched(do_nothing=True),
    ModelMerge)
assert_type(User.merge([(1, 'huey', 3)], fields=[User.id]), ModelMerge)
assert_type(User.merge(User.select()), ModelMerge)

# Database support flag is exposed uniformly; unsupported databases return
# False rather than raising at import time.
assert_type(real_db.supports_merge(), bool)
assert_type(real_db.merge_support, bool)
