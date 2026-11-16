import json

from ..errors import require
from ..values import fingerprint, utc_now
from .comparison import comparable_coverage, compare_periods
from .cubes import build_cubes
from .quality import quality_report, validate_result
from .windows import daily_grid, exposure_summary, match_order, request_windows
from .segments import profile_period, cross_shares
from .trends import daily_trends, weekday_seasonality
from .diagnostics import diagnostics
from .calendar_view import calendar_analysis
from .invariants import inspect_result


DEFAULT_DELTA_LIMIT = 5000

PERIOD_BLOCKS = ["orders", "exposure", "coverage", "cube", "profiles", "route_association",
                 "daily_trends", "weekday_seasonality", "diagnostics", "calendar_analysis"]


class FullRefreshRequired(Exception):
    """内部控制流：增量路径不支持本次变化，携带原因明确回退全量重算。"""

    def __init__(self, reason, details=None):
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}


def order_group(order):
    return {"origin": order["origin"], "destination": order["destination"],
            "travel_day": order["travel_day"]}


def reference_diff(before, after):
    changes = []
    groups = (("cities", "city"), ("sources", "source"), ("destinations", "destination"),
              ("calendars", "calendar"))
    for group, kind in groups:
        old = {item["id"]: item for item in before.get(group, [])}
        new = {item["id"]: item for item in after.get(group, [])}
        for identity in sorted(set(old) | set(new)):
            left = old.get(identity) or {}
            right = new.get(identity) or {}
            if left != right:
                changed = sorted(key for key in set(left) | set(right) if left.get(key) != right.get(key))
                changes.append({"kind": kind, "id": identity, "changed_fields": changed})
    return changes


def stored_shape_ok(stored):
    if not isinstance(stored, dict):
        return False
    if not isinstance(stored.get("request"), dict) or not isinstance(stored.get("data_revision"), int):
        return False
    if not isinstance(stored.get("references"), dict) or not isinstance(stored.get("lineage"), list):
        return False
    if not isinstance(stored.get("quality"), dict) or "comparison" not in stored:
        return False
    periods = stored.get("periods")
    if not isinstance(periods, dict) or not periods:
        return False
    for period in periods.values():
        if not isinstance(period, dict):
            return False
        for key in ("orders", "exposure", "coverage"):
            if not isinstance(period.get(key), list):
                return False
    return True


class Refresh:
    """以已持久化的分析结果为起点，只读取两个数据版本之间的变化推进分析。

    订单移到另一天、客源城市或目的地时，先撤回旧归属的贡献再计入新归属；
    校历、目录或覆盖状态变化即使不产生新订单，也会使受影响的统计范围被重算。
    """

    def __init__(self, database, catalog, calendars, coverage, intake, engine):
        self.database = database
        self.catalog = catalog
        self.calendars = calendars
        self.coverage = coverage
        self.intake = intake
        self.engine = engine

    def run(self, stored, target_revision, connection, delta_limit=DEFAULT_DELTA_LIMIT):
        if not stored_shape_ok(stored):
            raise FullRefreshRequired("stored_state_incomplete",
                                      {"note": "起点分析缺少增量所需的持久化结构"})
        request = stored["request"]
        from_revision = stored["data_revision"]
        windows = request_windows(request)
        sources = request["sources"]
        cities = request["cities"]

        order_revisions, new_heads = self.read_order_delta(request, from_revision, target_revision,
                                                           connection, delta_limit)
        coverage_delta = self.read_coverage_delta(sources, cities, from_revision, target_revision, connection)
        calendar_cities = self.read_calendar_delta(cities, from_revision, target_revision, connection)
        references = self.engine.validate_references(request, target_revision, connection)
        reference_changes = reference_diff(stored["references"], references)

        periods = {}
        scopes = {}
        order_changes = []
        before_index = {}
        for name, period in stored["periods"].items():
            for order in period["orders"]:
                before_index[(order["source"], order["order_id"])] = (name, order)
        any_dirty = False
        for name, window in windows.items():
            old = stored["periods"][name]
            kept = []
            withdrawn = []
            for order in old["orders"]:
                if (order["source"], order["order_id"]) in new_heads:
                    withdrawn.append(order)
                else:
                    kept.append(order)
            added = sorted((head for head in new_heads.values() if match_order(head, request, window)),
                           key=lambda order: (order["source"], order["order_id"]))
            orders = sorted(kept + added, key=lambda order: (order["source"], order["order_id"]))
            exposure = self.splice_exposure(old["exposure"], calendar_cities, window,
                                            target_revision, connection)
            matrix = self.splice_coverage(old["coverage"], coverage_delta)
            orders_changed = bool(withdrawn or added)
            coverage_changed = matrix != old["coverage"]
            exposure_changed = exposure != old["exposure"]
            dirty = orders_changed or coverage_changed or exposure_changed or bool(reference_changes)
            if dirty:
                period = self.rebuild_period(orders, exposure, matrix, window, request, references)
                blocks = list(PERIOD_BLOCKS)
            else:
                period = old
                blocks = []
            any_dirty = any_dirty or dirty
            periods[name] = period
            scopes[name] = self.scope_report(window, kept, withdrawn, added, coverage_delta,
                                             calendar_cities, coverage_changed,
                                             exposure_changed, dirty, blocks)
        for key, head in sorted(new_heads.items()):
            before = before_index.get(key)
            order_changes.append({"source": key[0], "order_id": key[1],
                                  "before_sequence": before[1]["sequence"] if before else None,
                                  "after_sequence": head["sequence"],
                                  "before_group": order_group(before[1]) if before else None,
                                  "after_group": order_group(head),
                                  "before_periods": [before[0]] if before else [],
                                  "after_periods": [name for name, window in windows.items()
                                                    if match_order(head, request, window)],
                                  "group_changed": bool(before) and order_group(before[1]) != order_group(head)})
        if any_dirty:
            lineage = []
            for name in windows:
                for order in periods[name]["orders"]:
                    lineage.append({"period": name, "source": order["source"], "order_id": order["order_id"],
                                    "event_id": order["event_id"], "sequence": order["sequence"],
                                    "revision": order["ingested_revision"], "digest": fingerprint(order)})
            comparison = None
            if "baseline" in periods:
                pairs = comparable_coverage(periods["current"]["coverage"], periods["baseline"]["coverage"],
                                            sources, cities)
                comparison = compare_periods(periods["current"]["cube"], periods["baseline"]["cube"],
                                             windows["current"], windows["baseline"], pairs)
            quality = quality_report(periods, request, comparison)
            derived = {"comparison": "recomputed", "quality": "recomputed", "lineage": "recomputed"}
        else:
            lineage = stored["lineage"]
            comparison = stored["comparison"]
            quality = stored["quality"]
            derived = {"comparison": "reused", "quality": "reused", "lineage": "reused"}
        result = {"format_version": 1, "data_revision": target_revision, "request": request,
                  "references": references, "periods": periods, "comparison": comparison,
                  "quality": quality, "lineage": lineage, "lineage_digest": fingerprint(lineage),
                  "calculated_at": utc_now(), "semantics": self.engine.semantics()}
        result["integrity"] = validate_result(result)
        result["statistical_checks"] = inspect_result(result)
        require(result["statistical_checks"]["ok"], "statistical_integrity", "统计口径一致性失败",
                details={"failures": result["statistical_checks"]["failures"]})
        require(result["integrity"]["ok"], "analysis_integrity", "分析内部一致性失败",
                details=result["integrity"])
        total_events = connection.execute(
            "SELECT COUNT(*) c FROM events WHERE revision<=? AND source IN (%s)"
            % ",".join("?" * len(sources)), [target_revision, *sources]).fetchone()["c"]
        report = {"read": {"order_revisions": order_revisions,
                           "order_events_scanned": len(order_revisions),
                           "order_events_total": total_events,
                           "coverage_cells_reread": len(coverage_delta),
                           "coverage_cells_total": sum(period["coverage_summary"]["expected_cells"]
                                                       for period in periods.values()),
                           "calendar_cities_reread": list(calendar_cities),
                           "calendar_cities_total": len(cities),
                           "reference_entries_changed": reference_changes},
                  "orders": {"touched": len(new_heads), "changes": order_changes,
                             "kept": sum(scope["orders"]["kept"] for scope in scopes.values())},
                  "scopes": scopes, "derived": derived}
        return result, report

    def read_order_delta(self, request, from_revision, target_revision, connection, delta_limit):
        sources = request["sources"]
        rows = connection.execute(
            "SELECT * FROM events WHERE revision>? AND revision<=? AND source IN (%s)"
            " ORDER BY source,order_id,sequence" % ",".join("?" * len(sources)),
            [from_revision, target_revision, *sources]).fetchall()
        if len(rows) > delta_limit:
            raise FullRefreshRequired("delta_events_exceeded",
                                      {"events": len(rows), "limit": delta_limit})
        revisions = []
        heads = {}
        for row in rows:
            revisions.append({"source": row["source"], "order_id": row["order_id"],
                              "sequence": row["sequence"], "event_id": row["event_id"],
                              "revision": row["revision"]})
            head = {**json.loads(row["payload"]), "source": row["source"],
                    "ingested_revision": row["revision"]}
            key = (row["source"], row["order_id"])
            if key not in heads or head["sequence"] > heads[key]["sequence"]:
                heads[key] = head
        return revisions, heads

    def read_coverage_delta(self, sources, cities, from_revision, target_revision, connection):
        rows = connection.execute(
            "SELECT * FROM coverage WHERE revision>? AND revision<=? AND source IN (%s) AND city IN (%s)"
            % (",".join("?" * len(sources)), ",".join("?" * len(cities))),
            [from_revision, target_revision, *sources, *cities]).fetchall()
        delta = {}
        for row in rows:
            cell = dict(row)
            key = (cell["source"], cell["city"], cell["day"])
            if key not in delta or cell["version"] > delta[key]["version"]:
                delta[key] = cell
        return delta

    def read_calendar_delta(self, cities, from_revision, target_revision, connection):
        rows = connection.execute(
            "SELECT DISTINCT city FROM calendars WHERE revision>? AND revision<=?",
            (from_revision, target_revision)).fetchall()
        return sorted({row["city"] for row in rows} & set(cities))

    def splice_exposure(self, stored_grid, calendar_cities, window, target_revision, connection):
        if not calendar_cities:
            return stored_grid
        fresh = {}
        for city in calendar_cities:
            rows = self.calendars.exposure([city], window.start, window.end, target_revision, connection)
            for row in daily_grid(window, [city], rows):
                fresh[(row["city"], row["day"])] = row
        return [fresh.get((row["city"], row["day"]), row) for row in stored_grid]

    def splice_coverage(self, stored_matrix, coverage_delta):
        if not coverage_delta:
            return stored_matrix
        return [coverage_delta.get((cell["source"], cell["city"], cell["day"]), cell)
                for cell in stored_matrix]

    def rebuild_period(self, orders, exposure, matrix, window, request, references):
        cube = build_cubes(orders, window, request["cities"], exposure)
        period = {"window": window.as_dict(), "orders": orders, "exposure": exposure,
                  "exposure_summary": exposure_summary(exposure), "coverage": matrix,
                  "coverage_summary": self.coverage.summary(matrix),
                  "coverage_gaps": self.coverage.missing_groups(matrix), "cube": cube}
        period["profiles"] = {dimension: profile_period(period, references, dimension)
                              for dimension in ("origin", "destination", "source")}
        period["route_association"] = cross_shares(orders, "origin", "destination")
        period["daily_trends"] = daily_trends(period)
        period["weekday_seasonality"] = weekday_seasonality(period)
        period["diagnostics"] = diagnostics(period)
        period["calendar_analysis"] = calendar_analysis(period, references)
        return period

    def scope_report(self, window, kept, withdrawn, added, coverage_delta, calendar_cities,
                     coverage_changed, exposure_changed, dirty, blocks):
        def cell(order, direction):
            return {"origin": order["origin"], "destination": order["destination"],
                    "travel_day": order["travel_day"], "direction": direction}
        cells = [cell(order, "withdrawn") for order in withdrawn]
        cells += [cell(order, "added") for order in added]
        cells.sort(key=lambda item: (item["travel_day"], item["origin"], item["destination"],
                                     item["direction"]))
        touched = withdrawn + added
        groups = {"origins": sorted({order["origin"] for order in touched}),
                  "destinations": sorted({order["destination"] for order in touched}),
                  "sources": sorted({order["source"] for order in touched})}
        coverage_cells = [{"source": key[0], "city": key[1], "day": key[2]}
                          for key in sorted(coverage_delta) if window.contains(key[2])]
        return {"recomputed": dirty, "blocks": blocks,
                "orders": {"kept": len(kept),
                           "withdrawn": [{"source": order["source"], "order_id": order["order_id"],
                                          "sequence": order["sequence"], "group": order_group(order)}
                                         for order in withdrawn],
                           "added": [{"source": order["source"], "order_id": order["order_id"],
                                      "sequence": order["sequence"], "group": order_group(order)}
                                     for order in added]},
                "order_cells": cells, "groups": groups,
                "coverage_cells": coverage_cells,
                "coverage_changed": coverage_changed,
                "calendar_cities": list(calendar_cities),
                "exposure_changed": exposure_changed}
