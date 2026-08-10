from production import subscribe
from shared import db as dbm


def test_empty_repository_has_no_subscriptions():
    conn = dbm.get_conn(":memory:")
    assert subscribe.load_subs(conn) == {}


def test_add_and_remove_use_database():
    conn = dbm.get_conn(":memory:")
    subscribe.add_sub("MSFT.US", conn=conn)
    assert subscribe.list_subs(conn)["count"] == 1
    subscribe.remove_sub("MSFT.US", conn=conn)
    assert subscribe.list_subs(conn)["count"] == 0
