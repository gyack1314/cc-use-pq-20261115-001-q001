import json
import uuid

from ..errors import conflict, missing, require
from ..values import canonical, fingerprint, identifier, integer, page_size, text, utc_now
from ..analysis.refresh import DEFAULT_DELTA_LIMIT, PERIOD_BLOCKS, FullRefreshRequired, reference_diff


class Advances:
    """把一份已完成分析推进到指定数据版本，并持久化推进过程供核查。

    推进请求以 advance_key 幂等：同一请求重试返回已保存结果，不重复计入；
    推进产物是新的分析版本，历史分析及其归档保持原样。
    """

    def __init__(self, database, editions, refresh, engine):
        self.database = database
        self.editions = editions
        self.refresh = refresh
        self.engine = engine

    def advance(self, source_id, advance_key, target_revision=None, delta_limit=None, actor="operator"):
        identifier(source_id)
        identifier(advance_key, "advance_key")
        actor = text(actor, "操作人", 100)
        if delta_limit is None:
            delta_limit = DEFAULT_DELTA_LIMIT
        integer(delta_limit, "delta_limit", 1, 100000)
        if target_revision is not None:
            integer(target_revision, "target_revision")
        request_digest = fingerprint({"source_edition_id": source_id, "advance_key": advance_key,
                                      "target_revision": target_revision, "delta_limit": delta_limit})
        with self.database.transaction(write=True) as connection:
            existing = connection.execute("SELECT * FROM advances WHERE advance_key=?",
                                          (advance_key,)).fetchone()
            if existing:
                if existing["request_digest"] != request_digest:
                    raise conflict("推进请求号已用于不同请求")
                return self.decode(existing, connection, replayed=True)
            source = self.editions.get(source_id, connection)
            require("revision" not in source["request"], "pinned_revision",
                    "固定数据版本的分析不能作为推进起点", 409)
            from_revision = source["data_revision"]
            target = self.database.snapshot_revision(connection, target_revision)
            require(target > from_revision, "invalid_target_revision",
                    "目标数据版本必须晚于起点分析的数据版本",
                    details={"from_revision": from_revision, "target_revision": target})
            request = source["request"]
            fallback = None
            try:
                result, partial = self.refresh.run(source["result"], target, connection, delta_limit)
                mode = "incremental"
            except FullRefreshRequired as exc:
                fallback = {"reason": exc.reason, "details": exc.details}
                result = self.engine.run({**request, "revision": target}, connection)
                result["request"] = request
                mode = "full"
                partial = self.full_partial(source["result"], result, request, connection, target)
            result_digest = fingerprint(result)
            report = {**partial, "mode": mode, "from_revision": from_revision, "to_revision": target,
                      "fallback": fallback,
                      "consistency": self.consistency(source["result"], result, result_digest)}
            clash = connection.execute("SELECT id FROM editions WHERE request_key=?",
                                       (advance_key,)).fetchone()
            require(clash is None, "advance_key_in_use", "推进请求号已被分析请求占用", 409)
            edition_id = "ed-" + uuid.uuid4().hex
            connection.execute(
                "INSERT INTO editions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (edition_id, advance_key, fingerprint(request), request["name"], target,
                 canonical(request), "draft", 1, canonical(result), result_digest, utc_now()))
            advance_id = "adv-" + uuid.uuid4().hex
            connection.execute(
                "INSERT INTO advances VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (advance_id, advance_key, source_id, edition_id, from_revision, target, mode,
                 canonical(fallback) if fallback else None, request_digest, canonical(report),
                 actor, utc_now()))
            row = connection.execute("SELECT * FROM advances WHERE id=?", (advance_id,)).fetchone()
            return self.decode(row, connection, replayed=False)

    def consistency(self, before, after, result_digest):
        return {"lineage_digest_before": before["lineage_digest"],
                "lineage_digest_after": after["lineage_digest"],
                "result_digest": result_digest,
                "integrity_ok": after["integrity"]["ok"],
                "statistical_checks_ok": after["statistical_checks"]["ok"],
                "reconciliation": {name: all(row["ok"] for row in period["cube"]["reconciliation"])
                                   for name, period in after["periods"].items()},
                "quality_publishable": after["quality"]["publishable"]}

    def full_partial(self, before, result, request, connection, target):
        sources = request["sources"]
        total_events = connection.execute(
            "SELECT COUNT(*) c FROM events WHERE revision<=? AND source IN (%s)"
            % ",".join("?" * len(sources)), [target, *sources]).fetchone()["c"]
        cells = sum(period["coverage_summary"]["expected_cells"] for period in result["periods"].values())
        return {"read": {"order_revisions": None,
                         "note": "全量重算读取了该来源截至目标版本的全部事件，未逐条列出",
                         "order_events_scanned": total_events, "order_events_total": total_events,
                         "coverage_cells_reread": cells, "coverage_cells_total": cells,
                         "calendar_cities_reread": sorted(request["cities"]),
                         "calendar_cities_total": len(request["cities"]),
                         "reference_entries_changed": reference_diff(before["references"],
                                                                     result["references"])},
                "orders": {"selected": sum(len(period["orders"]) for period in result["periods"].values())},
                "scopes": {name: {"recomputed": True, "blocks": list(PERIOD_BLOCKS)}
                           for name in result["periods"]},
                "derived": {"comparison": "recomputed", "quality": "recomputed",
                            "lineage": "recomputed"}}

    def decode(self, row, connection=None, replayed=False):
        result = dict(row)
        result["fallback"] = json.loads(row["fallback"]) if row["fallback"] else None
        result["report"] = json.loads(row["report"])
        result["replayed"] = replayed
        result["result_edition"] = self.editions.get(row["result_edition_id"], connection,
                                                     include_result=False)
        return result

    def get(self, advance_id):
        identifier(advance_id)
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM advances WHERE id=?", (advance_id,)).fetchone()
            if row is None:
                raise missing("推进记录", advance_id)
            return self.decode(row, connection)

    def list(self, edition_id=None, limit=100, offset=0):
        page_size(limit, offset)
        parameters = []
        clause = ""
        if edition_id is not None:
            identifier(edition_id)
            clause = " WHERE source_edition_id=? OR result_edition_id=?"
            parameters = [edition_id, edition_id]
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM advances" + clause + " ORDER BY created_at,id LIMIT ? OFFSET ?",
                parameters + [limit, offset]).fetchall()
            return [self.decode(row, connection) for row in rows]
