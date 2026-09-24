"""What the Services panel says a service is doing.

Both defects here reported a healthy cluster as something else, and neither
was visible as an error:

  * a one-shot experiment service that had run to completion reported
    'pending', because the task query filtered on `desired-state: running`
    and a finished task is not one. A finished run therefore sat in the
    dashboard's "Running Services" panel looking like it was about to start,
    until `cleanup` swept it.

  * a dropped SSH tunnel was turned into an empty list, so the panel emptied
    itself at random -- measured at six drops in a hundred seconds on this
    cluster -- and refilled two seconds later. An empty list and an
    unreachable manager were the same thing on screen.
"""

import unittest
from unittest import mock

import requests

from graflag.docker_ops import DockerManager, _service_status


def task(state, created="2026-09-23T07:00:00Z"):
    return {'Status': {'State': state}, 'CreatedAt': created}


class ServiceStatusFromTasks(unittest.TestCase):

    def test_a_running_task_is_running(self):
        self.assertEqual(_service_status(1, [task('running')]), 'running')

    def test_a_finished_one_shot_service_is_completed_not_pending(self):
        """The regression this file exists for."""
        self.assertEqual(_service_status(0, [task('complete')]), 'completed')

    def test_a_failed_task_is_failed(self):
        self.assertEqual(_service_status(0, [task('failed')]), 'failed')
        self.assertEqual(_service_status(0, [task('rejected')]), 'failed')

    def test_a_service_with_no_tasks_yet_is_pending(self):
        self.assertEqual(_service_status(0, []), 'pending')

    def test_a_scheduled_but_unstarted_task_is_pending(self):
        for state in ('new', 'allocated', 'assigned', 'preparing', 'starting'):
            self.assertEqual(_service_status(0, [task(state)]), 'pending', state)

    def test_the_newest_task_decides(self):
        """A restarted service keeps its old tasks; the latest is the truth."""
        tasks = [task('failed', '2026-09-23T07:00:00Z'),
                 task('complete', '2026-09-23T08:00:00Z')]
        self.assertEqual(_service_status(0, tasks), 'completed')
        self.assertEqual(_service_status(0, list(reversed(tasks))), 'completed')


class DroppedTunnelIsRetriedNotReported(unittest.TestCase):

    def _manager(self):
        mgr = DockerManager.__new__(DockerManager)
        mgr._lock = __import__('threading').RLock()
        mgr.close = mock.Mock()
        return mgr

    def test_a_dropped_connection_is_retried_once(self):
        mgr = self._manager()
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise requests.exceptions.ConnectionError(
                    "('Connection aborted.', RemoteDisconnected(...))")
            return 'listed'

        self.assertEqual(mgr._with_reconnect(flaky), 'listed')
        self.assertEqual(len(calls), 2, "the call was not retried")
        mgr.close.assert_called_once()      # the dead tunnel is torn down

    def test_a_working_call_is_not_retried_and_nothing_is_torn_down(self):
        mgr = self._manager()
        call = mock.Mock(return_value='listed')
        self.assertEqual(mgr._with_reconnect(call), 'listed')
        call.assert_called_once()
        mgr.close.assert_not_called()

    def test_a_real_error_still_surfaces(self):
        """Retrying must not turn a genuine failure into silence."""
        mgr = self._manager()
        call = mock.Mock(side_effect=ValueError("swarm is not initialised"))
        with self.assertRaises(ValueError):
            mgr._with_reconnect(call)
        call.assert_called_once()

    def test_a_connection_error_that_persists_is_raised(self):
        mgr = self._manager()
        call = mock.Mock(side_effect=requests.exceptions.ConnectionError("gone"))
        with self.assertRaises(requests.exceptions.ConnectionError):
            mgr._with_reconnect(call)
        self.assertEqual(call.call_count, 2)


class ApiDoesNotReportFailureAsEmpty(unittest.TestCase):

    def test_list_running_services_raises_instead_of_returning_empty(self):
        """[] used to mean both 'nothing running' and 'could not ask'."""
        from graflag.api import GraFlagAPI
        api = GraFlagAPI.__new__(GraFlagAPI)
        api.core = mock.Mock()
        api.core.list_services.side_effect = RuntimeError("tunnel is down")
        with self.assertRaises(RuntimeError):
            api.list_running_services()

    def test_an_empty_cluster_still_returns_an_empty_list(self):
        from graflag.api import GraFlagAPI
        api = GraFlagAPI.__new__(GraFlagAPI)
        api.core = mock.Mock()
        api.core.list_services.return_value = []
        self.assertEqual(api.list_running_services(), [])


if __name__ == '__main__':
    unittest.main()
