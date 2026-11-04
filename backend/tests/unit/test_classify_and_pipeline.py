import pytest

from app.database.document.pipeline import validate_pipeline
from app.errors import ValidationFailed
from app.query.classify import classify, classify_one, redact_literals, split_statements


def kinds(sql):
    return [s.kind for s in classify(sql)]


def test_splitting_respects_strings_comments_and_dollar_quotes():
    assert split_statements("SELECT 1; SELECT 2;") == ["SELECT 1", "SELECT 2"]
    assert split_statements("SELECT 'a;b'; SELECT \"x;y\"") == ["SELECT 'a;b'", 'SELECT "x;y"']
    assert split_statements("SELECT 1 -- ; DROP TABLE t\n; SELECT 2") == [
        "SELECT 1 -- ; DROP TABLE t",
        "SELECT 2",
    ]
    assert split_statements("SELECT /* ; */ 1; /* only a comment */") == ["SELECT /* ; */ 1"]
    assert split_statements("DO $$ BEGIN PERFORM 1; END $$; SELECT 1") == [
        "DO $$ BEGIN PERFORM 1; END $$",
        "SELECT 1",
    ]
    assert split_statements("SELECT 'it''s; fine'; SELECT 2") == [
        "SELECT 'it''s; fine'",
        "SELECT 2",
    ]
    assert split_statements("SELECT 'back\\'slash;'; SELECT 2") == [
        "SELECT 'back\\'slash;'",
        "SELECT 2",
    ]
    assert split_statements("  ;;  ") == []


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("SELECT * FROM t", "read"),
        ("  (SELECT 1)", "read"),
        ("select 1", "read"),
        ("-- hi\nSELECT 1", "read"),
        ("SHOW TABLES", "read"),
        ("EXPLAIN SELECT 1", "read"),
        ("EXPLAIN ANALYZE SELECT 1", "read"),
        ("EXPLAIN ANALYZE DELETE FROM t", "write"),
        ("WITH a AS (SELECT 1) SELECT * FROM a", "read"),
        ("WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d", "write"),
        ("SELECT * INTO newt FROM t", "write"),
        ("SELECT 'INTO' AS x", "read"),
        ("INSERT INTO t VALUES (1)", "write"),
        ("UPDATE t SET a = 1", "write"),
        ("CALL proc()", "write"),
        ("CREATE TABLE t (a int)", "ddl"),
        ("DROP TABLE t", "ddl"),
        ("GRANT ALL ON t TO u", "admin"),
        ("SET TRANSACTION READ WRITE", "session"),
        ("BEGIN", "session"),
        ("COMMIT", "session"),
        ("SET SESSION transaction_read_only = 0", "session"),
        ("RESET ALL", "session"),
        ("USE other", "session"),
        ("KILL 5", "session"),
        ("frobnicate things", "unknown"),
    ],
)
def test_classification(sql, kind):
    assert classify_one(sql).kind == kind


def test_keywords_inside_literals_and_comments_do_not_change_the_class():
    assert classify_one("SELECT 'DELETE FROM t; DROP TABLE x'").kind == "read"
    assert classify_one("SELECT 1 /* INSERT INTO t */").kind == "read"
    assert classify_one("/* leading */ DELETE FROM t WHERE a = 1").kind == "write"


@pytest.mark.parametrize(
    ("sql", "destructive"),
    [
        ("DROP TABLE t", True),
        ("TRUNCATE t", True),
        ("DELETE FROM t", True),
        ("DELETE FROM t WHERE a = 1", False),
        ("UPDATE t SET a = 1", True),
        ("UPDATE t SET a = 1 WHERE id = 2", False),
        ("ALTER TABLE t DROP COLUMN a", True),
        ("ALTER TABLE t ADD COLUMN a int", False),
        ("SELECT 1", False),
        ("INSERT INTO t VALUES (1)", False),
    ],
)
def test_destructive_detection(sql, destructive):
    assert classify_one(sql).destructive is destructive


def test_multi_statement_kinds():
    assert kinds("SELECT 1; INSERT INTO t VALUES (1); BEGIN") == ["read", "write", "session"]


def test_literal_redaction_keeps_structure_only():
    out = redact_literals(
        "SELECT * FROM users WHERE email = 'a@b.c' AND age > 30 AND x = 1.5e3 -- note 42"
    )
    assert out == "SELECT * FROM users WHERE email = ? AND age > ? AND x = ? -- note 42"
    assert "a@b.c" not in out
    assert (
        redact_literals("SELECT t1.col2 FROM t1") == "SELECT t1.col2 FROM t1"
    )  # identifiers untouched
    assert redact_literals("SELECT $$secret$$, $tag$s2$tag$") == "SELECT ?, ?"


# ----------------------------------------------------------------------------- pipelines


def ok(p, limit=100):
    return validate_pipeline(p, limit)


def bad(p):
    with pytest.raises(ValidationFailed):
        validate_pipeline(p, 100)


def test_allowed_pipeline_gets_a_final_limit():
    stages, targets = ok(
        [{"$match": {"a": {"$gt": 1}}}, {"$group": {"_id": "$a", "n": {"$sum": 1}}}]
    )
    assert stages[-1] == {"$limit": 101} and targets == set()
    stages2, _ = ok([{"$match": {"a": 1}}, {"$limit": 5}])
    assert stages2[-1] == {"$limit": 5}  # a smaller user limit is kept
    stages3, _ = ok([{"$limit": 5000}])
    assert stages3[-1] == {"$limit": 101}


def test_extended_json_values_are_converted():
    stages, _ = ok([{"$match": {"_id": {"$oid": "507f1f77bcf86cd799439011"}}}])
    from bson import ObjectId

    assert stages[0]["$match"]["_id"] == ObjectId("507f1f77bcf86cd799439011")


@pytest.mark.security
@pytest.mark.parametrize(
    "pipeline",
    [
        [{"$out": "stolen"}],
        [{"$merge": {"into": "x"}}],
        [{"$match": {"a": 1}}, {"$out": "x"}],
        [
            {
                "$addFields": {
                    "x": {"$function": {"body": "function(){return 1}", "args": [], "lang": "js"}}
                }
            }
        ],
        [
            {
                "$group": {
                    "_id": None,
                    "x": {
                        "$accumulator": {
                            "init": "function(){}",
                            "accumulate": "function(){}",
                            "accumulateArgs": [],
                            "merge": "function(){}",
                            "lang": "js",
                        }
                    },
                }
            }
        ],
        [{"$match": {"$where": "sleep(1000)"}}],
        [{"$match": {"$expr": {"$function": {}}}}],
        [{"$facet": {"a": [{"$out": "x"}]}}],
        [{"$lookup": {"from": "c", "pipeline": [{"$merge": {"into": "z"}}], "as": "o"}}],
        [{"$unionWith": {"coll": "c", "pipeline": [{"$out": "z"}]}}],
        [{"$project": {"x": {"$cond": [True, {"$function": {}}, 1]}}}],
        [{"$currentOp": {}}],
        [{"$collStats": {}}],
        [{"$listSessions": {}}],
        [{"$bogus": 1}],
        [{"$match": {"a": 1}, "$limit": 1}],
        [],
        "not json",
        {"$match": {}},
        [{"$match": {"$expr": {"$gt": ["$a", 1]}}}],  # $expr is not allowed in $match
        [{"$limit": -1}],
        [{"$sample": {}}],
        [
            {
                "$graphLookup": {
                    "from": "c",
                    "startWith": "$a",
                    "connectFromField": "a",
                    "connectToField": "b",
                    "as": "x",
                }
            }
        ],
        [{"$lookup": {"from": {"db": "admin", "coll": "users"}, "as": "x"}}],
        [{"$lookup": {"from": "system.users", "localField": "a", "foreignField": "b", "as": "x"}}],
        [{"$unionWith": 5}],
    ],
)
def test_dangerous_or_unsupported_pipelines_are_rejected(pipeline):
    bad(pipeline)


def test_lookup_like_stages_report_their_target_collections():
    _, targets = ok(
        [
            {"$lookup": {"from": "orders", "localField": "a", "foreignField": "b", "as": "o"}},
            {"$unionWith": "archive"},
            {"$unionWith": {"coll": "more", "pipeline": [{"$match": {"x": 1}}]}},
            {
                "$graphLookup": {
                    "from": "tree",
                    "startWith": "$a",
                    "connectFromField": "a",
                    "connectToField": "b",
                    "as": "t",
                    "maxDepth": 3,
                }
            },
            {
                "$facet": {
                    "f": [
                        {
                            "$lookup": {
                                "from": "nested",
                                "localField": "a",
                                "foreignField": "b",
                                "as": "n",
                            }
                        }
                    ]
                }
            },
        ]
    )
    assert targets == {"orders", "archive", "more", "tree", "nested"}


def test_complexity_limits():
    bad([{"$match": {"a": 1}}] * 31)
    deep = [{"$match": {"a": 1}}]
    for _ in range(6):
        deep = [{"$lookup": {"from": "c", "pipeline": deep, "as": "x"}}]
    bad(deep)
    bad([{"$facet": {f"f{i}": [{"$limit": 1}] for i in range(11)}}])
