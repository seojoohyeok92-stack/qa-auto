from __future__ import annotations

from typing import Any

from repositories.database import Database


OUTCOMES_CTE = """
WITH outcomes AS (
  SELECT pr.inquiry_id, pr.answer_draft_id, i.inquiry_type,
         av.posted_at,
         CASE WHEN EXISTS (
           SELECT 1 FROM answer_versions cv
           WHERE cv.inquiry_id=pr.inquiry_id
             AND cv.version_kind='NAVER_CORRECTION_APPLIED'
         ) THEN 'CORRECTED'
         WHEN pr.status='REVIEWED_NO_CHANGE' THEN 'UNCHANGED'
         ELSE 'PENDING' END AS outcome
  FROM post_reviews pr
  JOIN inquiries i ON i.id=pr.inquiry_id
  JOIN answer_versions av ON av.id=pr.initial_version_id
  WHERE av.version_kind='AUTO_POST_INITIAL' AND av.naver_status='POSTED'
)
"""


class LearningPerformanceRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _rate(unchanged: int, corrected: int) -> float | None:
        total = int(unchanged) + int(corrected)
        return round(int(unchanged) * 100 / total, 1) if total else None

    @staticmethod
    def _percentage(numerator: int, denominator: int) -> float | None:
        return (
            round(int(numerator) * 100 / int(denominator), 1)
            if int(denominator)
            else None
        )

    def quality_period(
        self, *, start_days: int, end_days: int = 0
    ) -> dict[str, Any]:
        """Aggregate operational KPIs from the actual inbound inquiry cohort.

        Date boundaries are evaluated as KST calendar datetimes in SQLite.
        An answer with no explicit no-change observation is deliberately not
        counted as "unmodified"; it remains outside the correction cohort.
        """

        start = f"-{int(start_days)} days"
        end = f"-{int(end_days)} days"
        range_sql = (
            "julianday({column})>=julianday('now', '+9 hours', ?) "
            "AND (?=0 OR julianday({column})<julianday('now', '+9 hours', ?))"
        )
        with self.database.connection() as connection:
            row = connection.execute(
                """
                WITH processed AS (
                  SELECT DISTINCT id inquiry_id
                  FROM inquiries
                  WHERE """
                + range_sql.format(column="datetime(COALESCE(source_created_at, created_at), '+9 hours')")
                + """
                ), generated AS (
                  SELECT DISTINCT inquiry_id
                  FROM answer_drafts
                  WHERE is_active=1
                    AND TRIM(COALESCE(final_answer, edited_answer, original_answer, ''))<>''
                ), review_required AS (
                  SELECT DISTINCT inquiry_id
                  FROM activity_logs
                  WHERE event_code IN (
                    'AUTO_PROCESSING_REVIEW_REQUIRED',
                    'AUTO_PROCESSING_BLOCKED',
                    'AUTO_POST_BLOCKED_DPS_SESSION',
                    'AUTO_POST_SKIPPED_POLICY_BLOCKED'
                  ) AND """
                + range_sql.format(column="datetime(created_at, '+9 hours')")
                + """
                ), posted AS (
                  SELECT DISTINCT inquiry_id
                  FROM naver_post_attempts
                  WHERE status='POSTED' AND auto_post_run_id IS NOT NULL
                )
                SELECT COUNT(*) processed,
                       SUM(EXISTS(
                         SELECT 1 FROM generated g
                         WHERE g.inquiry_id=p.inquiry_id
                       )) generated,
                       SUM(EXISTS(
                         SELECT 1 FROM posted a
                         WHERE a.inquiry_id=p.inquiry_id
                       )) auto_posted,
                       SUM(EXISTS(
                         SELECT 1 FROM review_required r
                         WHERE r.inquiry_id=p.inquiry_id
                       )) review_required
                FROM processed p
                """,
                (
                    start, int(end_days), end,
                    start, int(end_days), end,
                ),
            ).fetchone()
        outcome = self.operator_correction_period(
            start_days=start_days, end_days=end_days
        )
        processed = int(row["processed"] or 0)
        generated = int(row["generated"] or 0)
        auto_posted = int(row["auto_posted"] or 0)
        review_required = int(row["review_required"] or 0)
        return {
            "processed": processed,
            "generated": generated,
            "auto_posted": auto_posted,
            "review_required": review_required,
            "generation_rate": self._percentage(generated, processed),
            "auto_post_rate": self._percentage(auto_posted, processed),
            "review_required_rate": self._percentage(
                review_required, processed
            ),
            "correction_rate": outcome["correction_rate"],
            "correction_known": outcome["known"],
            "corrected": outcome["corrected"],
            "correction_pending": outcome["pending"],
        }

    def operator_correction_period(
        self, *, start_days: int, end_days: int = 0
    ) -> dict[str, Any]:
        """Measured employee modifications, including Naver sync corrections.

        ``REVIEWED_NO_CHANGE`` is the only durable no-edit observation.  A
        staff edit or ``NAVER_CORRECTION_APPLIED`` is a measured correction;
        all other historical answers are intentionally unknown.
        """
        range_sql = (
            "julianday(datetime(COALESCE(i.source_created_at,i.created_at), '+9 hours'))>=julianday('now', '+9 hours', ?) "
            "AND (?=0 OR julianday(datetime(COALESCE(i.source_created_at,i.created_at), '+9 hours'))<julianday('now', '+9 hours', ?))"
        )
        with self.database.connection() as connection:
            row = connection.execute(
                """
                WITH cohort AS (
                  SELECT i.id FROM inquiries i WHERE """ + range_sql + """
                ), corrected AS (
                  SELECT DISTINCT av.inquiry_id
                  FROM answer_versions av
                  JOIN cohort c ON c.id=av.inquiry_id
                  WHERE av.version_kind IN (
                    'NAVER_CORRECTION_APPLIED', 'STAFF_CORRECTION_DRAFT'
                  )
                  UNION
                  SELECT DISTINCT d.inquiry_id FROM answer_drafts d
                  JOIN cohort c ON c.id=d.inquiry_id
                  WHERE TRIM(COALESCE(d.edited_answer,''))<>''
                    AND TRIM(COALESCE(d.edited_answer,''))<>TRIM(COALESCE(d.original_answer,''))
                ), unchanged AS (
                  SELECT DISTINCT pr.inquiry_id FROM post_reviews pr
                  JOIN cohort c ON c.id=pr.inquiry_id
                  WHERE pr.status='REVIEWED_NO_CHANGE'
                    AND NOT EXISTS (SELECT 1 FROM corrected x WHERE x.inquiry_id=pr.inquiry_id)
                )
                SELECT (SELECT COUNT(*) FROM corrected) corrected,
                       (SELECT COUNT(*) FROM unchanged) unchanged,
                       (SELECT COUNT(*) FROM cohort) total
                """,
                (f"-{int(start_days)} days", int(end_days), f"-{int(end_days)} days"),
            ).fetchone()
        corrected, unchanged = int(row["corrected"] or 0), int(row["unchanged"] or 0)
        return {
            "corrected": corrected, "unchanged": unchanged,
            "known": corrected + unchanged,
            "pending": max(0, int(row["total"] or 0) - corrected - unchanged),
            "correction_rate": self._rate(corrected, unchanged),
            "unchanged_rate": self._rate(unchanged, corrected),
        }

    def learning_counts(self) -> dict[str, int]:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT SUM(CASE WHEN active=1 THEN 1 ELSE 0 END) active,
                       SUM(CASE WHEN created_at>=datetime('now','-7 days') THEN 1 ELSE 0 END) new_7,
                       SUM(CASE WHEN created_at>=datetime('now','-30 days') THEN 1 ELSE 0 END) new_30
                FROM learning_examples
                """
            ).fetchone()
            historical = connection.execute(
                """
                SELECT SUM(active=1) active,
                       SUM(active=1 AND created_at>=datetime('now','-7 days')) new_7,
                       SUM(active=1 AND created_at>=datetime('now','-30 days')) new_30
                FROM historical_cases
                """
            ).fetchone()
        learning = {
            key: int(row[key] or 0)
            for key in ("active", "new_7", "new_30")
        }
        verified = {
            key: int(historical[key] or 0)
            for key in ("active", "new_7", "new_30")
        }
        return {
            key: learning[key] + verified[key]
            for key in ("active", "new_7", "new_30")
        } | {
            "learning_examples_active": learning["active"],
            "historical_verified_active": verified["active"],
        }

    def outcome_period(self, *, start_days: int, end_days: int = 0) -> dict[str, Any]:
        start = f"-{int(start_days)} days"
        with self.database.connection() as connection:
            row = connection.execute(
                OUTCOMES_CTE + """
                SELECT SUM(outcome='UNCHANGED') unchanged,
                       SUM(outcome='CORRECTED') corrected,
                       SUM(outcome='PENDING') pending
                FROM outcomes
                WHERE julianday(posted_at)>=julianday('now', ?)
                  AND (?=0 OR julianday(posted_at)<julianday('now', ?))
                """,
                (start, int(end_days), f"-{int(end_days)} days"),
            ).fetchone()
        unchanged, corrected = int(row["unchanged"] or 0), int(row["corrected"] or 0)
        return {
            "unchanged": unchanged, "corrected": corrected,
            "pending": int(row["pending"] or 0),
            "known": unchanged + corrected,
            "unchanged_rate": self._rate(unchanged, corrected),
            "correction_rate": self._rate(corrected, unchanged),
        }

    def provenance_effect(self) -> dict[str, Any]:
        with self.database.connection() as connection:
            generated = int(connection.execute(
                """
                SELECT COUNT(DISTINCT answer_draft_id)
                FROM answer_learning_provenance
                WHERE answer_draft_id IS NOT NULL AND included_in_prompt=1
                  AND reference_kind='LEARNING'
                """
            ).fetchone()[0])
            generated_historical = int(connection.execute(
                """
                SELECT COUNT(DISTINCT answer_draft_id)
                FROM answer_learning_provenance
                WHERE answer_draft_id IS NOT NULL AND included_in_prompt=1
                  AND reference_kind='HISTORICAL'
                """
            ).fetchone()[0])
            rows = connection.execute(
                OUTCOMES_CTE + """
                SELECT CASE WHEN EXISTS (
                         SELECT 1 FROM answer_learning_provenance p
                         WHERE p.answer_draft_id=o.answer_draft_id
                           AND p.reference_kind='LEARNING'
                           AND p.included_in_prompt=1
                       ) THEN 'USED' ELSE 'NOT_USED' END cohort,
                       SUM(o.outcome='UNCHANGED') unchanged,
                       SUM(o.outcome='CORRECTED') corrected
                FROM outcomes o WHERE o.outcome IN ('UNCHANGED','CORRECTED')
                GROUP BY cohort
                """
            ).fetchall()
        result: dict[str, Any] = {
            "generated_with_learning": generated,
            "generated_with_historical": generated_historical,
        }
        for cohort in ("USED", "NOT_USED"):
            row = next((item for item in rows if item["cohort"] == cohort), None)
            unchanged = int(row["unchanged"] or 0) if row else 0
            corrected = int(row["corrected"] or 0) if row else 0
            result[cohort.lower()] = {
                "unchanged": unchanged, "corrected": corrected,
                "sample": unchanged + corrected,
                "unchanged_rate": self._rate(unchanged, corrected),
            }
        return result

    def source_rows(self) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                OUTCOMES_CTE + """,
                categorized AS (
                  SELECT le.*,
                    CASE
                      WHEN json_extract(le.metadata_json,'$.source_origin')='HISTORICAL_PROMOTED' THEN 'HISTORICAL_PROMOTED'
                      WHEN json_extract(le.metadata_json,'$.source_origin')='COPILOT_CORRECTION' THEN 'COPILOT_CORRECTION'
                      WHEN le.learning_source='AUTO_POST_CORRECTED' AND EXISTS (
                        SELECT 1 FROM answer_versions av
                        WHERE av.id=CAST(json_extract(le.metadata_json,'$.answer_version_id') AS INTEGER)
                          AND av.finalization_source='NAVER_DIRECT_EDIT_SYNC'
                      ) THEN 'NAVER_DIRECT_EDIT'
                      WHEN le.learning_source='AUTO_POST_CORRECTED' THEN 'STAFF_POST_CORRECTION'
                      WHEN le.learning_source='AUTO_POST_REVIEWED_NO_CHANGE'
                       AND json_extract(le.metadata_json,'$.acceptance_mode')='AUTO_OBSERVATION' THEN 'POSITIVE_LEARNING'
                      WHEN le.learning_source='AUTO_POST_REVIEWED_NO_CHANGE' THEN 'REVIEWED_NO_CHANGE'
                      WHEN le.learning_source='APPROVED_EDITED' THEN 'STAFF_EDITED'
                      ELSE le.learning_source END source_group
                  FROM learning_examples le
                )
                SELECT c.source_group,
                       SUM(c.active=1) active_count,
                       SUM(c.created_at>=datetime('now','-30 days')) new_30,
                       COUNT(DISTINCT p.answer_draft_id) referenced_answers,
                       COUNT(DISTINCT CASE WHEN o.outcome='UNCHANGED' THEN o.inquiry_id END) unchanged,
                       COUNT(DISTINCT CASE WHEN o.outcome='CORRECTED' THEN o.inquiry_id END) corrected
                FROM categorized c
                LEFT JOIN answer_learning_provenance p
                  ON p.learning_example_id=c.id AND p.included_in_prompt=1
                LEFT JOIN outcomes o ON o.answer_draft_id=p.answer_draft_id
                GROUP BY c.source_group ORDER BY active_count DESC
                """
            ).fetchall()
            historical = connection.execute(
                OUTCOMES_CTE + """
                SELECT 'HISTORICAL_VERIFIED_LEARNING' source_group,
                       (SELECT COUNT(*) FROM historical_cases WHERE active=1) active_count,
                       (SELECT COUNT(*) FROM historical_cases
                        WHERE created_at>=datetime('now','-30 days')) new_30,
                       COUNT(DISTINCT p.answer_draft_id) referenced_answers,
                       COUNT(DISTINCT CASE WHEN o.outcome='UNCHANGED' THEN o.inquiry_id END) unchanged,
                       COUNT(DISTINCT CASE WHEN o.outcome='CORRECTED' THEN o.inquiry_id END) corrected
                FROM historical_cases hc
                LEFT JOIN answer_learning_provenance p
                  ON p.historical_case_id=hc.id AND p.included_in_prompt=1
                LEFT JOIN outcomes o ON o.answer_draft_id=p.answer_draft_id
                """
            ).fetchone()
            if historical is not None and any(
                int(historical[key] or 0)
                for key in ("active_count", "new_30", "referenced_answers")
            ):
                rows = [*rows, historical]
        result = []
        for raw in rows:
            row = dict(raw)
            row["active_count"] = int(row["active_count"] or 0)
            row["new_30"] = int(row["new_30"] or 0)
            row["referenced_answers"] = int(row["referenced_answers"] or 0)
            row["unchanged"] = int(row["unchanged"] or 0)
            row["corrected"] = int(row["corrected"] or 0)
            row["unchanged_rate"] = self._rate(row["unchanged"], row["corrected"])
            result.append(row)
        return result

    def type_quality(self) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                OUTCOMES_CTE + """
                SELECT COALESCE(inquiry_type,'미분류') inquiry_type,
                       SUM(outcome='UNCHANGED') unchanged,
                       SUM(outcome='CORRECTED') corrected
                FROM outcomes WHERE outcome IN ('UNCHANGED','CORRECTED')
                GROUP BY COALESCE(inquiry_type,'미분류')
                ORDER BY (SUM(outcome='UNCHANGED')+SUM(outcome='CORRECTED')) DESC
                """
            ).fetchall()
        result = []
        for raw in rows:
            unchanged, corrected = int(raw["unchanged"] or 0), int(raw["corrected"] or 0)
            result.append({
                "inquiry_type": raw["inquiry_type"],
                "sample": unchanged + corrected,
                "unchanged_rate": self._rate(unchanged, corrected),
            })
        return result

    def trend(self) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                OUTCOMES_CTE + """
                SELECT date(posted_at) period,
                       SUM(outcome='UNCHANGED') unchanged,
                       SUM(outcome='CORRECTED') corrected
                FROM outcomes
                WHERE outcome IN ('UNCHANGED','CORRECTED')
                  AND julianday(posted_at)>=julianday('now','-60 days')
                GROUP BY date(posted_at) ORDER BY period
                """
            ).fetchall()
        return [{
            "period": row["period"],
            "unchanged_rate": self._rate(int(row["unchanged"] or 0), int(row["corrected"] or 0)),
            "sample": int(row["unchanged"] or 0) + int(row["corrected"] or 0),
        } for row in rows]

    def correction_trend(self, *, days: int) -> list[dict[str, Any]]:
        """Return the measured-employee-correction trend for the inbound cohort.

        This intentionally shares the same correction evidence contract as the
        KPI card: internal staff edits and ``NAVER_CORRECTION_APPLIED`` count;
        only ``REVIEWED_NO_CHANGE`` proves an unchanged answer.  Unknown
        historical rows are omitted rather than silently treated as no-edit.
        """

        days = int(days)
        period_sql = (
            "date(datetime(COALESCE(i.source_created_at,i.created_at), '+9 hours'))"
            if days <= 30
            else "strftime('%Y-%W', datetime(COALESCE(i.source_created_at,i.created_at), '+9 hours'))"
        )
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                WITH cohort AS (
                  SELECT i.id, {period_sql} AS period
                  FROM inquiries i
                  WHERE julianday(datetime(COALESCE(i.source_created_at,i.created_at), '+9 hours'))
                        >= julianday('now', '+9 hours', ?)
                ), corrected AS (
                  SELECT DISTINCT av.inquiry_id
                  FROM answer_versions av
                  JOIN cohort c ON c.id=av.inquiry_id
                  WHERE av.version_kind IN ('NAVER_CORRECTION_APPLIED','STAFF_CORRECTION_DRAFT')
                  UNION
                  SELECT DISTINCT d.inquiry_id
                  FROM answer_drafts d
                  JOIN cohort c ON c.id=d.inquiry_id
                  WHERE TRIM(COALESCE(d.edited_answer,''))<>''
                    AND TRIM(COALESCE(d.edited_answer,''))<>TRIM(COALESCE(d.original_answer,''))
                ), unchanged AS (
                  SELECT DISTINCT pr.inquiry_id
                  FROM post_reviews pr
                  JOIN cohort c ON c.id=pr.inquiry_id
                  WHERE pr.status='REVIEWED_NO_CHANGE'
                    AND NOT EXISTS (SELECT 1 FROM corrected x WHERE x.inquiry_id=pr.inquiry_id)
                )
                SELECT c.period,
                       SUM(EXISTS(SELECT 1 FROM corrected x WHERE x.inquiry_id=c.id)) corrected,
                       SUM(EXISTS(SELECT 1 FROM unchanged x WHERE x.inquiry_id=c.id)) unchanged
                FROM cohort c
                WHERE EXISTS(SELECT 1 FROM corrected x WHERE x.inquiry_id=c.id)
                   OR EXISTS(SELECT 1 FROM unchanged x WHERE x.inquiry_id=c.id)
                GROUP BY c.period ORDER BY c.period
                """,
                (f"-{days} days",),
            ).fetchall()
        return [
            {
                "period": str(row["period"]),
                "correction_rate": self._rate(
                    int(row["corrected"] or 0),
                    int(row["unchanged"] or 0),
                ),
                "sample": int(row["unchanged"] or 0)
                + int(row["corrected"] or 0),
            }
            for row in rows
        ]

    def positive_observation(self, observation_days: int) -> dict[str, Any]:
        modifier = f"-{int(observation_days)} days"
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT
                  SUM(julianday(av.posted_at)>julianday('now', ?)
                      AND av.naver_status='POSTED') observing,
                  SUM(julianday(av.posted_at)<=julianday('now', ?)) due,
                  SUM(julianday(av.posted_at)<=julianday('now', ?)
                      AND (pr.status='POST_UNKNOWN' OR av.naver_status='POST_UNKNOWN')) unknown_count,
                  SUM(julianday(av.posted_at)<=julianday('now', ?)
                      AND (d.validation_status LIKE 'FAIL%' OR d.validation_status LIKE '%REVIEW%')) validator_excluded
                FROM post_reviews pr
                JOIN answer_versions av ON av.id=pr.initial_version_id
                LEFT JOIN answer_drafts d ON d.id=pr.answer_draft_id
                WHERE av.version_kind='AUTO_POST_INITIAL'
                """,
                (modifier, modifier, modifier, modifier),
            ).fetchone()
            converted = int(connection.execute(
                """
                SELECT COUNT(*) FROM learning_examples
                WHERE learning_source='AUTO_POST_REVIEWED_NO_CHANGE'
                  AND json_extract(metadata_json,'$.acceptance_mode')='AUTO_OBSERVATION'
                """
            ).fetchone()[0])
            corrected = int(connection.execute(
                "SELECT COUNT(DISTINCT inquiry_id) FROM learning_examples WHERE learning_source='AUTO_POST_CORRECTED'"
            ).fetchone()[0])
        due = int(row["due"] or 0)
        known_excluded = corrected + int(row["unknown_count"] or 0) + int(row["validator_excluded"] or 0)
        return {
            "observation_days": int(observation_days),
            "observing": int(row["observing"] or 0), "due": due,
            "converted": converted,
            "corrected": corrected,
            "unknown": int(row["unknown_count"] or 0),
            "validator": int(row["validator_excluded"] or 0),
            "unconfirmed_or_other": max(0, due - converted - known_excluded),
        }

    def correction_summary(self) -> dict[str, Any]:
        types = (
            "INQUIRY_CLASSIFICATION_CORRECTION", "REQUIRED_ACTION_CORRECTION",
            "ANSWER_POLICY_CORRECTION", "RESPONSE_CORRECTION",
        )
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT json_extract(metadata_json,'$.correction_type') correction_type,
                       COUNT(*) total,
                       SUM(created_at>=datetime('now','-30 days')) new_30
                FROM learning_examples
                WHERE json_extract(metadata_json,'$.source_origin')='COPILOT_CORRECTION'
                GROUP BY json_extract(metadata_json,'$.correction_type')
                """
            ).fetchall()
            inquiries_30 = int(connection.execute(
                "SELECT COUNT(*) FROM inquiries WHERE julianday(registered_at)>=julianday('now','-30 days')"
            ).fetchone()[0])
        mapped = {str(row["correction_type"]): dict(row) for row in rows}
        return {
            "types": [{
                "correction_type": value,
                "total": int((mapped.get(value) or {}).get("total") or 0),
                "new_30": int((mapped.get(value) or {}).get("new_30") or 0),
            } for value in types],
            "inquiries_30": inquiries_30,
        }
