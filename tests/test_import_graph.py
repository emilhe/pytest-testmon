import pytest

from testmon.process_code import (
    MODULE_LEVEL,
    create_fingerprint_source,
    match_fingerprint_source,
)

pytest_plugins = ("pytester",)


class TestModuleLevelFingerprint:
    def test_module_level_without_covered_lines(self):
        fingerprint = create_fingerprint_source(
            """\
            CONSTANT = 1
            def f():
                return 2
            """,
            {MODULE_LEVEL},
        )
        assert not match_fingerprint_source(
            """\
            CONSTANT = 2
            def f():
                return 2
            """,
            fingerprint,
        )
        assert match_fingerprint_source(
            """\
            CONSTANT = 1
            def f():
                return 3
            """,
            fingerprint,
        )

    def test_empty_module_matches_nothing(self):
        fingerprint = create_fingerprint_source("", {MODULE_LEVEL})
        assert not match_fingerprint_source("CONSTANT = 1\n", fingerprint)


def run(testdir, *args):
    return testdir.runpytest_inprocess("--testmon", "-v", *args)


@pytest.fixture
def testdir(pytester):
    pytester.makepyfile(
        constants="CONSTANT = 1\n",
        models="""
            from constants import CONSTANT

            class Model:
                default = CONSTANT

            def unused():
                return 1
        """,
        lazy="VALUE = 1\n",
        fixture_only="VALUE = 1\n",
    )
    pytester.makeconftest(
        """
        import pytest

        @pytest.fixture(scope="session")
        def value():
            import fixture_only

            return fixture_only.VALUE
        """
    )
    pytester.makepyfile(
        test_models="""
            from models import Model

            def test_model():
                assert Model.default == 1
        """,
        test_lazy="""
            import importlib

            def test_lazy():
                assert importlib.import_module("lazy").VALUE == 1
        """,
        test_fixture="""
            def test_first(value):
                assert value == 1

            def test_second(value):
                assert value == 1

            def test_without():
                pass
        """,
    )
    run(pytester).assert_outcomes(passed=5)
    return pytester


def test_unchanged(testdir):
    run(testdir).assert_outcomes()


def test_transitive_module_level_change(testdir):
    testdir.makepyfile(constants="CONSTANT = 22\n")
    run(testdir).assert_outcomes(failed=1)


def test_function_body_change_is_not_module_level(testdir):
    testdir.makepyfile(
        models="""
            from constants import CONSTANT

            class Model:
                default = CONSTANT

            def unused():
                return 22
        """
    )
    run(testdir).assert_outcomes()


def test_import_module_in_test(testdir):
    testdir.makepyfile(lazy="VALUE = 22\n")
    run(testdir).assert_outcomes(failed=1)


def test_session_fixture_import_reaches_every_user(testdir):
    testdir.makepyfile(fixture_only="VALUE = 22\n")
    # test_second reads the cached value, test_without never asked for it.
    run(testdir).assert_outcomes(failed=2)


def test_xdist(testdir):
    pytest.importorskip("xdist")
    testdir.makepyfile(fixture_only="VALUE = 22\n")
    run(testdir, "-n", "2").assert_outcomes(failed=2)
