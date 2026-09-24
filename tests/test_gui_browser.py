"""End-to-end checks against the rendered dashboard.

Everything else about the GUI is asserted structurally, because this
workstation's Chrome extension cannot reach 127.0.0.1. That gap is exactly
where the worst bug of this round lived: the server was paginating correctly,
the API returned five, and the page rendered fifty -- because the WebSocket
handler pushed its own fixed `limit=50` array straight over the rendered list
on connect. No amount of reading the Python would have shown that.

These start a real server, drive a real browser, and assert what is on screen.
Skipped when selenium or a browser is unavailable, so the suite still runs
anywhere.
"""

import contextlib
import os
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

PAGE_SIZE = 5


def _free_port():
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _driver():
    """Headless Firefox, or None when the stack is unavailable."""
    try:
        from selenium import webdriver
        from selenium.webdriver.firefox.options import Options
    except Exception:
        return None
    opts = Options()
    opts.add_argument("--headless")
    opts.add_argument("--width=1400")
    opts.add_argument("--height=1400")
    try:
        return webdriver.Firefox(options=opts)
    except Exception:
        return None


def _server_reachable(port, timeout=45):
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(1)
    return False


_PROC = None
_DRIVER = None
_PORT = None


def setUpModule():
    """One server and one browser for every class in this file.

    Starting them per class doubles a suite that already takes a minute.
    """
    global _PROC, _DRIVER, _PORT
    if not os.environ.get("GRAFLAG_BROWSER_TESTS"):
        return                                  # the classes skip themselves
    _PORT = _free_port()
    root = Path(__file__).resolve().parents[1]
    _PROC = subprocess.Popen(
        [sys.executable, "-m", "graflag.cli", "gui",
         "--host", "127.0.0.1", "--port", str(_PORT)],
        cwd=str(root), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not _server_reachable(_PORT):
        _PROC.kill()
        raise unittest.SkipTest("gui did not come up (no cluster?)")
    _DRIVER = _driver()
    if _DRIVER is None:
        _PROC.kill()
        raise unittest.SkipTest("no usable browser")


def tearDownModule():
    if _DRIVER:
        _DRIVER.quit()
    if _PROC:
        _PROC.kill()


class _Dashboard(unittest.TestCase):
    """Loads the dashboard and waits for the first render."""

    @property
    def driver(self):
        return _DRIVER

    @property
    def port(self):
        return _PORT

    def setUp(self):
        self.driver.get(f"http://127.0.0.1:{self.port}/")
        time.sleep(7)          # initial load + the first socket tick


@unittest.skipUnless(os.environ.get("GRAFLAG_BROWSER_TESTS"),
                     "set GRAFLAG_BROWSER_TESTS=1 (needs a cluster and a browser)")
class RenderedDashboard(_Dashboard):

    def _rows(self):
        return self.driver.find_elements(
            "css selector", ".experiments-table-side tbody tr")

    def _names(self):
        return [e.text for e in self.driver.find_elements(
            "css selector", ".experiments-table-side tbody tr td:first-child")]

    def _panel_text(self):
        return self.driver.find_element(
            "css selector", ".experiments-split").text

    def test_the_page_renders(self):
        self.assertEqual(self.driver.title, "GraFlag Dashboard")

    def test_the_table_shows_one_page_not_the_whole_window(self):
        """The regression this file exists for.

        The socket handler pushed `list_experiments(limit=50)` as a bare array
        and the client assigned it straight into the rendered list, so the
        dashboard asked for five and showed fifty from the first render.
        """
        self.assertLessEqual(
            len(self._rows()), PAGE_SIZE,
            "more rows than a page: something is overwriting the paginated "
            "list, most likely the WebSocket push")

    def test_the_row_count_survives_socket_ticks(self):
        """The updater fires every two seconds; a push of the full list would
        replace the page on the first tick."""
        before = len(self._rows())
        time.sleep(6)                      # at least two ticks
        self.assertEqual(before, len(self._rows()),
                         "a socket tick changed how many rows are rendered")

    def test_the_footer_counts_the_share_not_the_page(self):
        """`experiments.length` is one page under server-side paging, so the
        old arithmetic rendered '6-5 of 5' on page two."""
        import re
        text = self._panel_text()
        m = re.search(r"(\d+)-(\d+) of (\d+)", text)
        self.assertIsNotNone(m, f"no range in the footer: {text[:200]!r}")
        start, end, total = (int(g) for g in m.groups())
        self.assertEqual(start, 1)
        self.assertGreaterEqual(end, start)
        self.assertGreaterEqual(total, end,
                                "the total is smaller than the range shown")

    def test_next_page_loads_different_experiments(self):
        first = self._names()
        buttons = self.driver.find_elements(
            "css selector", ".experiments-table-side .btn-page")
        if len(buttons) < 2:
            self.skipTest("no pagination controls (fewer than two pages)")
        buttons[-1].click()
        time.sleep(4)
        second = self._names()
        self.assertTrue(second, "page two rendered no rows")
        self.assertNotEqual(first, second, "page two repeated page one")

    def test_no_severe_console_errors(self):
        try:
            logs = self.driver.get_log("browser")
        except Exception:
            self.skipTest("this driver exposes no console log")
        severe = [l for l in logs if l.get("level") == "SEVERE"]
        self.assertEqual(severe, [], f"console errors: {severe[:3]}")


# Columns whose text is free-form and is meant to elide behind a `title`
# tooltip. Every other column must be wide enough for what it renders.
ELIDING_COLUMNS = {"Description", "Experiment"}

# Content width of a cell, independent of the column it sits in: the markup is
# cloned into an off-screen probe that is allowed to be as wide as it likes.
_CONTENT_NEEDS = """
function need(cell){
  const cs = getComputedStyle(cell);
  const probe = document.createElement('div');
  probe.style.cssText = 'position:absolute;left:-9999px;top:0;visibility:hidden;'
                      + 'white-space:nowrap;width:max-content;';
  probe.style.font = cs.font;
  probe.innerHTML = cell.innerHTML;
  document.body.appendChild(probe);
  const w = probe.getBoundingClientRect().width;
  probe.remove();
  return w + parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight);
}
const t = arguments[0].querySelector('table');
const heads = [...t.querySelectorAll('thead th')];
const needs = heads.map(need);
t.querySelectorAll('tbody tr:not(.filler-row)').forEach(tr => {
  [...tr.children].forEach((td, i) => {
    if (i < needs.length) needs[i] = Math.max(needs[i], need(td));
  });
});
return {
  labels: heads.map(h => h.textContent.trim()),
  widths: heads.map(h => Math.round(h.getBoundingClientRect().width * 10) / 10),
  needs:  needs.map(n => Math.round(n * 10) / 10),
  height: Math.round(t.querySelector('tbody').getBoundingClientRect().height * 10) / 10,
  first:  (t.querySelector('tbody tr') || {}).textContent || ''
};
"""


@unittest.skipUnless(os.environ.get("GRAFLAG_BROWSER_TESTS"),
                     "set GRAFLAG_BROWSER_TESTS=1 (needs a cluster and a browser)")
class TableGeometry(_Dashboard):
    """The tables must not move when you page through them.

    Browsers size columns from the content they can see, so page two of
    Methods -- all `bond_*` names, all one description length -- produced
    different columns from page one. Measured before the fix: columns shifted
    up to 93.4px and the body changed height by 89.6px, so the row under the
    cursor moved out from under it on every page change. `table-layout: fixed`
    plus a declared `<colgroup>` is what pins them; filler rows pin the height.
    """

    def _tables(self):
        return self.driver.find_elements("css selector", ".table-container")

    def _measure(self, container):
        return self.driver.execute_script(_CONTENT_NEEDS, container)

    def _next_page(self, container):
        """Advance one page; False when this table has only one."""
        buttons = container.find_elements("css selector", ".btn-page")
        if len(buttons) < 2 or not buttons[-1].is_enabled():
            return False
        buttons[-1].click()
        time.sleep(3)
        return True

    def test_columns_and_height_hold_still_across_pages(self):
        checked = 0
        for index in range(len(self._tables())):
            container = self._tables()[index]
            before = self._measure(container)
            if not self._next_page(container):
                continue
            after = self._measure(self._tables()[index])
            # Without this the test passes on a table that never turned the
            # page -- which is exactly how a first attempt at it fooled me.
            self.assertNotEqual(
                before["first"], after["first"],
                f"{before['labels']}: the page did not actually change")
            checked += 1
            for label, was, now in zip(before["labels"], before["widths"],
                                       after["widths"]):
                self.assertAlmostEqual(
                    was, now, delta=1.0,
                    msg=f"column {label!r} moved {abs(now - was):.1f}px "
                        f"between pages ({was} -> {now})")
            self.assertAlmostEqual(
                before["height"], after["height"], delta=1.0,
                msg=f"{before['labels']}: the table changed height by "
                    f"{abs(after['height'] - before['height']):.1f}px")
        if not checked:
            self.skipTest("no table has more than one page")

    def test_no_column_is_narrower_than_what_it_renders(self):
        """Declaring widths trades one failure for another: a column that is
        too narrow clips silently. `Runs` did, at 14% -- `reimplementation`
        overflowed it by 30px and read as `reimplementatio`.
        """
        too_narrow = []
        for index in range(len(self._tables())):
            container = self._tables()[index]
            while True:
                m = self._measure(container)
                for label, width, need in zip(m["labels"], m["widths"],
                                              m["needs"]):
                    if label in ELIDING_COLUMNS or not label:
                        continue
                    if need > width + 1:
                        too_narrow.append(
                            f"{label!r} renders {need}px into {width}px")
                if not self._next_page(container):
                    break
                container = self._tables()[index]
        self.assertEqual(too_narrow, [],
                         "columns too narrow for their content: "
                         + "; ".join(sorted(set(too_narrow))))
