"""Tests for the stat-constant confinement checker (T9, #730).

Phase 2's closing ratchet: designated stat coefficients (the TS%/TOV% free-throw
term 0.44, and the Hollinger Game Score weights) may appear only under
`app/services/stats/`. Without a mechanical guard, "the eight copies regrow the
next time someone needs a formula in a query"
(docs/plans/programmatic-code-discipline.md §1.3). These tests pin: both rules
firing on the shapes that actually caused the duplication, both rules staying
quiet on the false-positive shapes verified against this codebase while building
the checker, the one frozen exemption resolving against the real registry, and
the vacuity check that keeps that exemption honest.
"""

from __future__ import annotations

import ast
from pathlib import Path
from textwrap import dedent

from tests.unit._script_loader import load_script


checker = load_script("check_stat_constants")


def _violations(source: str, path: str = "app/services/summer_league_widget.py"):
    return checker.find_violations(Path(path), dedent(source))


class TestTsTovCoefficientRule:
    """Rule 1a: 0.44 is flagged unconditionally outside the engine package."""

    def test_bare_0_44_outside_the_package_is_flagged(self):
        """The founding failure shape: the coefficient hand-copied into a query."""
        found = _violations(
            """
            def turnover_rate(fga, fta, tov):
                return tov / (fga + 0.44 * fta + tov)
            """
        )
        assert len(found) == 1
        assert found[0].code == "R1"
        assert "0.44" in found[0].message

    def test_negated_0_44_is_still_flagged(self):
        """A unary minus wraps the same Constant node; it must not hide it."""
        found = _violations(
            """
            def f(fta):
                return -0.44 * fta
            """
        )
        assert len(found) == 1

    def test_0_44_outside_a_multiplication_is_still_flagged(self):
        """Rule 1a is unconditional -- a comparison, not just a coefficient use."""
        found = _violations(
            """
            def is_high_usage(rate):
                return rate > 0.44
            """
        )
        assert len(found) == 1

    def test_each_offending_site_is_reported_separately(self):
        found = _violations(
            """
            def f(a, b):
                x = a + 0.44 * b
                y = b + 0.44 * a
            """
        )
        assert len(found) == 2
        assert [v.lineno for v in found] == [3, 4]

    def test_waiver_with_a_reason_silences_it(self):
        found = _violations(
            """
            def f(fga, fta, tov):
                return fga + 0.44 * fta + tov  # discipline: stat-constants legacy shim, see #999
            """
        )
        assert found == []

    def test_waiver_without_a_reason_is_rejected(self):
        """A bare marker must not silence the check -- exceptions have to be argued."""
        found = _violations(
            """
            def f(fga, fta, tov):
                return fga + 0.44 * fta + tov  # discipline: stat-constants
            """
        )
        assert len(found) == 1


class TestGameScoreCooccurrenceRule:
    """Rule 1b: the Game Score weights are flagged only on co-occurrence.

    Individually, 0.4/0.7/0.3 are ordinary scale factors elsewhere in this
    codebase (test-fixture distributions, a notability score) -- verified by
    grep while building this checker. A bare per-value rule would flag both and
    train people to bypass it, so this only fires when several are multiplied
    together inside one connected arithmetic expression.
    """

    def test_three_or_more_weights_multiplied_together_is_flagged(self):
        """The actual Game Score shape: several weights chained by +/-."""
        found = _violations(
            """
            def gmsc(fgm, fga, oreb):
                return 0.4 * fgm - 0.7 * fga + 0.3 * oreb
            """
        )
        assert len(found) == 3
        assert all(v.code == "R1" for v in found)

    def test_two_weights_do_not_meet_the_cooccurrence_threshold(self):
        found = _violations(
            """
            def f(fgm, fga):
                return 0.4 * fgm - 0.7 * fga
            """
        )
        assert found == []

    def test_a_single_weight_alone_is_not_flagged(self):
        """The real desk_facts.py shape: one scale factor, not a formula."""
        found = _violations(
            """
            def notability(of):
                return 0.6 + 0.4 * of
            """
        )
        assert found == []

    def test_weights_isolated_across_call_boundaries_are_not_grouped(self):
        """The real environment_fixtures.py shape: each weight sits in its own int(...)."""
        found = _violations(
            """
            def dist(players):
                return (
                    players
                    - int(players * 0.45)
                    - int(players * 0.3)
                    - int(players * 0.15)
                )
            """
        )
        assert found == []

    def test_waiver_silences_a_cooccurrence_group(self):
        found = _violations(
            """
            def gmsc(fgm, fga, oreb):
                # discipline: stat-constants scratch experiment, not shipped
                return 0.4 * fgm - 0.7 * fga + 0.3 * oreb
            """
        )
        assert found == []


class TestStringPatternRule:
    """Rule 2: designated-coefficient arithmetic embedded in string/f-string text.

    What an AST float-literal check cannot see -- SQL built as Python text holds
    the formula as characters, not a numeric node. This is the exact shape
    ``_game_score_sql`` had in app.services.summer_league_explorer_service before
    this ticket folded it into the registry.
    """

    def test_fstring_coefficient_multiplication_is_flagged(self):
        found = _violations(
            """
            def game_score_sql(box):
                return (
                    f"{box('pts')} + 0.4 * {box('fgm')} - 0.7 * {box('fga')} "
                    f"+ 0.3 * {box('oreb')}"
                )
            """
        )
        assert len(found) == 1
        assert found[0].code == "R2"

    def test_plain_sql_string_with_the_ts_tov_coefficient_is_flagged(self):
        """A raw (non-f-string) SQL aggregate literal holding the coefficient as text."""
        found = _violations(
            """
            SQL = "SUM(tov) / (SUM(fga) + 0.44 * SUM(fta) + SUM(tov))"
            """
        )
        assert len(found) == 1
        assert found[0].code == "R2"

    def test_docstring_mentioning_the_formula_is_not_flagged(self):
        """The real environment_service.py:993 shape -- prose, not code."""
        found = _violations(
            '''
            def f():
                """Computed as ``FGA + 0.44*FTA + TOV``, independently."""
                return 1
            '''
        )
        assert found == []

    def test_declaration_prose_keyword_is_not_flagged(self):
        """A MetricDefinition's formula= text is promoted, not debt (env_registry.py:374-384)."""
        found = _violations(
            """
            MetricDefinition(
                key="turnover_rate",
                formula="sum(tov) / (sum(fga) + 0.44 * sum(fta) + sum(tov))",
                denominator="field-goal attempts + 0.44 * free-throw attempts",
            )
            """
        )
        assert found == []

    def test_unrelated_multiplication_in_a_string_is_not_flagged(self):
        found = _violations(
            """
            SQL = "price * quantity"
            """
        )
        assert found == []

    def test_a_designated_looking_value_with_extra_digits_is_not_flagged(self):
        """Word-boundary matching: 0.440 or 0.4400 must not read as 0.44/0.4."""
        found = _violations(
            """
            SQL = "threshold * 0.4400"
            """
        )
        assert found == []

    def test_waiver_silences_a_string_pattern_violation(self):
        found = _violations(
            """
            def game_score_sql(box):
                # discipline: stat-constants scratch experiment, not shipped
                return f"{box('pts')} + 0.4 * {box('fgm')} - 0.7 * {box('fga')} + 0.3 * {box('oreb')}"
            """
        )
        assert found == []


class TestRegistryFormulaReappearanceRule:
    """Rule 3 (R4, T10/#741): exact reappearance of a registry-declared metric's
    SQL text, whether or not it carries a designated coefficient.

    The blocker #730 declined a generic ``SUM(<box field>)`` sweep over: once
    efg_pct/fg3ar/ftr moved into the registry (T10), retyping their SQL text
    outside app/services/stats/ is exactly the shape this rule exists to catch.
    fg_pct/fg3_pct/ft_pct remain permanently un-registered (#726) and must stay
    silent -- proving the rule is derived from the registry, not a blanket sweep.
    """

    def test_fg3ar_row_grain_text_retyped_outside_the_package_is_flagged(self):
        """The exact shape T10 migrated: a plain string duplicating fg3ar_sql_text."""
        found = _violations(
            """
            _pct_exprs = {
                "fg3ar": "fg3a * 1.0 / NULLIF(fga, 0)",
            }
            """
        )
        assert len(found) == 1
        assert found[0].code == "R4"
        assert "fg3ar_sql_text" in found[0].message

    def test_ftr_aggregate_grain_text_retyped_outside_the_package_is_flagged(self):
        found = _violations(
            """
            _pct_exprs = {
                "ftr": "SUM(fta) * 1.0 / NULLIF(SUM(fga), 0)",
            }
            """
        )
        assert len(found) == 1
        assert found[0].code == "R4"
        assert "ftr_sql_text" in found[0].message

    def test_efg_pct_text_interpolated_via_box_calls_is_not_flagged(self):
        """``box(...)`` calls are interpolations, not literal text: the joined
        literal segments (``" + 0.5 * "``, ``") / NULLIF("``, ...) never contain a
        whole registered formula string on their own. This is the *correct*
        box-callable shape (matching efg_pct_sql_text itself), not a duplicate --
        rule 3 only fires on a fixed-field-name literal like T10's ten sites had."""
        found = _violations(
            """
            def efg_sql(box):
                return f"({box('fgm')} + 0.5 * {box('fg3m')}) / NULLIF({box('fga')}, 0)"
            """
        )
        assert found == []

    def test_fg_pct_text_is_never_flagged(self):
        """fg_pct is permanently out of scope (#726) -- no registered *_sql_text
        function emits its text, so no allowlist entry is needed to keep it quiet."""
        found = _violations(
            """
            _pct_exprs = {
                "fg_pct": "SUM(fgm) * 1.0 / NULLIF(SUM(fga), 0)",
            }
            """
        )
        assert found == []

    def test_calling_the_registry_function_is_not_flagged(self):
        """The correct call-site shape -- calling fg3ar_sql_text -- is code, not a
        string literal duplicating its output, so rule 3 does not see it at all."""
        found = _violations(
            """
            from app.services.stats.registry import fg3ar_sql_text

            _pct_exprs = {
                "fg3ar": fg3ar_sql_text(lambda c: c),
            }
            """
        )
        assert found == []

    def test_docstring_quoting_the_formula_is_not_flagged(self):
        found = _violations(
            '''
            def f():
                """Emits ``fg3a * 1.0 / NULLIF(fga, 0)`` -- fg3ar's raw form."""
                return 1
            '''
        )
        assert found == []

    def test_waiver_silences_a_registry_reappearance_violation(self):
        found = _violations(
            """
            _pct_exprs = {
                "fg3ar": "fg3a * 1.0 / NULLIF(fga, 0)",  # discipline: stat-constants legacy shim, see #999
            }
            """
        )
        assert found == []

    def test_known_registry_formula_texts_cover_the_t10_metrics(self):
        """Sanity-checks the registry-introspection helper actually discovers the
        three T10 functions (plus the pre-existing ones), not just an empty list."""
        names = {name for name, _grain, _text in checker._known_registry_formula_texts()}
        assert {"efg_pct_sql_text", "fg3ar_sql_text", "ftr_sql_text"} <= names


class TestPackageScoping:
    """The same literal inside app/services/stats/ is not scanned at all."""

    def test_engine_package_paths_are_recognized(self):
        assert checker._in_engine_package("app/services/stats/formulas.py") is True
        assert checker._in_engine_package("app/services/stats/registry.py") is True
        assert (
            checker._in_engine_package("app/services/summer_league_explorer_service.py")
            is False
        )

    def test_designated_literals_inside_the_engine_package_are_not_scanned(self):
        """Sanity-checks the exclusion does real work: formulas.py has 0.44 and is excluded."""
        formulas_path = checker.REPO_ROOT / "app/services/stats/formulas.py"
        assert "0.44" in formulas_path.read_text(encoding="utf-8")
        assert formulas_path not in checker._iter_app_python_files()


class TestFrozenExemptionAllowlist:
    """The one allowlist entry is read from app.services.stats.registry, not hand-written."""

    def test_frozen_exemption_is_discovered_from_the_registry(self):
        sites, malformed = checker._parse_exemption_sites()
        assert malformed == []
        assert [s.metric_key for s in sites] == ["environment_turnover_rate"]
        site = sites[0]
        assert site.cited_path == "app/services/summer_league_environment_service.py"

    def test_the_frozen_site_resolves_against_the_real_tree(self):
        """At HEAD, the exemption is not vacuous and suppresses exactly its declared site."""
        sites, _ = checker._parse_exemption_sites()
        exempted, vacuous = checker._resolve_exemptions(sites)
        assert vacuous == []
        assert exempted == {("app/services/summer_league_environment_service.py", 1716)}

    def test_allowlisted_site_is_not_reported_by_the_full_check(self):
        """check() suppresses whatever _resolve_exemptions matched in the full pipeline."""
        found = checker.check([])
        offending = [
            v
            for v in found
            if v.path == "app/services/summer_league_environment_service.py"
        ]
        assert offending == []

    def test_vacuity_check_fails_when_the_cited_site_no_longer_matches(
        self, tmp_path, monkeypatch
    ):
        """A registered exemption whose cited site no longer matches must fail (vacuity)."""
        fake_file = tmp_path / "fake_module.py"
        fake_file.write_text("def f(fga, fta, tov):\n    return fga + fta + tov\n")
        monkeypatch.setattr(checker, "REPO_ROOT", tmp_path)

        site = checker._ExemptionSite(
            metric_key="fake_metric",
            cited_path="fake_module.py",
            cited_line=1,
            reason="fake_module.py:1 pretends to be frozen",
        )
        exempted, vacuous = checker._resolve_exemptions([site])
        assert exempted == set()
        assert len(vacuous) == 1
        assert vacuous[0].code == "R3"
        assert "rotted" in vacuous[0].message

    def test_vacuity_check_tolerates_the_real_off_by_one_between_citation_and_code(
        self, tmp_path, monkeypatch
    ):
        """Reproduces the real citation shape: comment cited, code one line below it."""
        fake_file = tmp_path / "fake_module.py"
        fake_file.write_text(
            "def f(fga, fta, tov):\n"
            "    # Frozen contract formula\n"
            "    return fga + 0.44 * fta + tov\n"
        )
        monkeypatch.setattr(checker, "REPO_ROOT", tmp_path)

        site = checker._ExemptionSite(
            metric_key="fake_metric",
            cited_path="fake_module.py",
            cited_line=2,
            reason="fake_module.py:2 frozen",
        )
        exempted, vacuous = checker._resolve_exemptions([site])
        assert vacuous == []
        assert ("fake_module.py", 3) in exempted

    def test_vacuity_check_fails_when_the_cited_file_does_not_exist(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(checker, "REPO_ROOT", tmp_path)
        site = checker._ExemptionSite(
            metric_key="fake_metric",
            cited_path="does_not_exist.py",
            cited_line=1,
            reason="does_not_exist.py:1 frozen",
        )
        exempted, vacuous = checker._resolve_exemptions([site])
        assert exempted == set()
        assert len(vacuous) == 1

    def test_malformed_citation_is_treated_as_vacuous(self, monkeypatch):
        """An unparseable exemption_reason citation cannot be verified, so it is vacuous."""
        from app.services.stats.registry import MetricDefinition

        template = next(iter(checker.frozen_exemptions()))
        fake = MetricDefinition(
            metric_key="fake",
            metric_family=template.metric_family,
            unit=template.unit,
            denominator="n/a",
            definition_version="0.0.0",
            requires=(),
            formula="n/a",
            rollup_class=template.rollup_class,
            grain_validity=(),
            comparison_semantics="n/a",
            allowed_reference_kinds=(),
            minimum_sample_rule="n/a",
            coverage_requirement="n/a",
            interpretation_note="n/a",
            is_frozen_exemption=True,
            exemption_reason="no citation here at all",
        )
        monkeypatch.setattr(checker, "frozen_exemptions", lambda: (fake,))

        sites, malformed = checker._parse_exemption_sites()
        assert sites == []
        assert len(malformed) == 1
        assert malformed[0].code == "R3"
        assert "fake" in malformed[0].message


class TestSyntaxErrorHandling:
    def test_unparseable_source_is_reported_not_raised(self):
        found = _violations("def f(:\n    pass\n")
        assert len(found) == 1
        assert found[0].code == "R0"


class TestRepoState:
    """The checker must pass its own guard -- vacuous otherwise."""

    def test_the_whole_tree_check_is_clean_at_head(self):
        assert [v.format() for v in checker.check([])] == []

    def test_the_confinement_package_still_contains_the_coefficient(self):
        """Guards against a clean run because the package emptied out, not consolidated."""
        formulas = (checker.REPO_ROOT / "app/services/stats/formulas.py").read_text(
            encoding="utf-8"
        )
        registry = (checker.REPO_ROOT / "app/services/stats/registry.py").read_text(
            encoding="utf-8"
        )
        assert "0.44" in formulas
        assert "0.44" in registry
        tree = ast.parse(
            (checker.REPO_ROOT / "app/services/stats/formulas.py").read_text(
                encoding="utf-8"
            )
        )
        assert any(
            isinstance(n, ast.Constant) and n.value in (0.4, 0.7, 0.3)
            for n in ast.walk(tree)
        )
