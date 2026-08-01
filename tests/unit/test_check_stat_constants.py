"""Tests for the stat-constant confinement checker (T9, #730).

Phase 2's closing ratchet: designated stat coefficients (the TS%/TOV% free-throw
term 0.44, and the Hollinger Game Score weights), registered SQL text, and
registered SQLAlchemy expression shapes may appear only under `app/services/stats/`.
Without a mechanical guard, "the eight copies regrow the next time someone needs a formula in a query"
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


class TestEfgThreePointWeightRule:
    """Rule 1c: eFG%'s 0.5 is flagged only against a three-point-makes operand.

    Added by the Phase 2 QA gate (#731). 0.5 is far too common a float to flag
    on sight, so the rule is keyed to the operand it multiplies. It exists
    because T6 bound the Explorer's raw-SQL-text eFG% forms to the registry but
    left its three SQLAlchemy-expression filter sites hand-written: rule 1 was
    blind (0.5 was not designated) and rule 4 was blind (it matches only
    *string* literals), so the engine and the filter could silently disagree on
    the weight with the whole suite green. Reproduced before this rule existed.
    """

    def test_plain_python_efg_numerator_is_flagged(self):
        """The bare-arithmetic shape: `0.5 * fg3m` inside an eFG% numerator."""
        found = _violations(
            """
            def efg(fgm, fg3m, fga):
                return 100.0 * (fgm + 0.5 * fg3m) / fga
            """
        )
        assert len(found) == 1
        assert found[0].code == "R1"
        assert "eFG%" in found[0].message

    def test_sqlalchemy_expression_shape_is_flagged(self):
        """The exact notation that slipped past T6 -- attribute access, not a name."""
        found = _violations(
            """
            def where(ps, func):
                return 100.0 * (ps.fgm + 0.5 * ps.fg3m) / func.nullif(ps.fga, 0)
            """
        )
        assert len(found) == 1
        assert found[0].code == "R1"

    def test_aggregate_grain_func_sum_shape_is_flagged(self):
        """The career-grain notation: the operand is wrapped in ``func.sum(...)``."""
        found = _violations(
            """
            def having(ps, func):
                return (func.sum(ps.fgm) + 0.5 * func.sum(ps.fg3m))
            """
        )
        assert len(found) == 1

    def test_getattr_indirection_shape_is_flagged(self):
        """``getattr(table, "fg3m")`` -- the grain indirection the registry uses."""
        found = _violations(
            """
            def build(table):
                return getattr(table, "fgm") + 0.5 * getattr(table, "fg3m")
            """
        )
        assert len(found) == 1

    def test_reversed_operand_order_is_flagged(self):
        """``fg3m * 0.5`` is the same formula written the other way round."""
        found = _violations(
            """
            def efg(fgm, fg3m, fga):
                return (fgm + fg3m * 0.5) / fga
            """
        )
        assert len(found) == 1

    def test_half_against_an_unrelated_operand_is_not_flagged(self):
        """0.5 is an ordinary scale factor -- no three-point operand, no violation."""
        found = _violations(
            """
            def midpoint(low, high):
                return low + 0.5 * (high - low)
            """
        )
        assert found == []

    def test_half_added_rather_than_multiplied_is_not_flagged(self):
        """Only the multiplicative shape is the eFG% weight."""
        found = _violations(
            """
            def f(fg3m):
                return fg3m + 0.5
            """
        )
        assert found == []

    def test_an_inline_waiver_suppresses_it(self):
        """The documented escape hatch works here as it does for the other rules."""
        found = _violations(
            """
            def efg(fgm, fg3m, fga):
                # discipline: stat-constants one-off, not the shared eFG% path
                return (fgm + 0.5 * fg3m) / fga
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
    """Rule 3 (R4, T10/#741): exact reappearance of a registry-declared metric's.

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

    def test_efg_pct_text_interpolated_via_box_calls_is_flagged(self):
        """Formatted ``box(...)`` columns are retained for R4 comparison."""
        found = _violations(
            """
            def efg_sql(box):
                return f"({box('fgm')} + 0.5 * {box('fg3m')}) / NULLIF({box('fga')}, 0)"
            """
        )
        assert len(found) == 1
        assert found[0].code == "R4"

    def test_ftr_text_interpolated_via_box_calls_is_flagged(self):
        """Coefficient-free f-string formulas are checked too."""
        found = _violations(
            """
            def ftr_sql(box):
                return f"{box('fta')} * 1.0 / NULLIF({box('fga')}, 0)"
            """
        )
        assert len(found) == 1
        assert found[0].code == "R4"

    def test_fg_pct_text_is_never_flagged(self):
        """fg_pct is permanently out of scope (#726).

        No registered ``*_sql_text`` function emits its text, so no allowlist entry is
        needed to keep it quiet.
        """
        found = _violations(
            """
            _pct_exprs = {
                "fg_pct": "SUM(fgm) * 1.0 / NULLIF(SUM(fga), 0)",
            }
            """
        )
        assert found == []

    def test_calling_the_registry_function_is_not_flagged(self):
        """The correct call-site shape is code, not a duplicate string literal.

        Calling ``fg3ar_sql_text`` means rule 3 does not see the emitted output at all.
        """
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
        """Sanity-check registry introspection discovers the three T10 functions.

        The pre-existing functions must be discovered too, rather than returning an
        empty list.
        """
        names = {name for name, _grain, _text in checker._known_registry_formula_texts()}
        assert {"efg_pct_sql_text", "fg3ar_sql_text", "ftr_sql_text"} <= names


class TestRegistryExpressionReappearanceRule:
    """Rule 4 (R5, #745): registry SQLAlchemy arithmetic cannot be retyped.

    The arithmetic cannot be retyped outside the engine package.

    The test uses ``astd_pct`` because it has no designated numeric coefficient;
    that proves the new rule is comparing an introspected arithmetic shape rather
    than merely repeating R1c's eFG% constant check.
    """

    def test_astd_expression_duplicate_is_flagged(self):
        """A hand-written ORM expression matching the registry form goes red."""
        found = _violations(
            """
            def having(ps):
                return ps.ast_fgm + ps.unast_fgm
            """
        )
        assert len(found) == 1
        assert found[0].code == "R5"
        assert "astd_pct_denom_expr" in found[0].message

    def test_aggregate_column_wrappers_match_the_same_registry_shape(self):
        """Row and ``func.sum`` aggregate leaves share one registry declaration."""
        found = _violations(
            """
            def having(ps, func):
                return func.sum(ps.ast_fgm) + func.sum(ps.unast_fgm)
            """
        )
        assert len(found) == 1
        assert found[0].code == "R5"

    def test_unregistered_shooting_split_shape_is_not_flagged(self):
        """fg_pct remains outside the registry-derived expression family."""
        found = _violations(
            """
            def shooting_split(ps, func):
                return func.sum(ps.fgm) / func.nullif(func.sum(ps.fga), 0)
            """
        )
        assert found == []

    def test_expression_waiver_suppresses_a_registry_shape(self):
        found = _violations(
            """
            def having(ps):
                # discipline: stat-constants legacy expression, see #746
                return ps.ast_fgm + ps.unast_fgm
            """
        )
        assert found == []

    def test_expression_functions_are_discovered_from_the_registry(self):
        """The R5 surface is introspected rather than hand-maintained."""
        names = set(checker._registry_expression_functions())
        assert names == {
            "astd_pct_denom_expr",
            "efg_pct_num_expr",
            "tov_pct_denom_expr",
            "ts_pct_denom_expr",
        }


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
