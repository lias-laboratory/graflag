"""What `graflag clear` decides to delete, and what it refuses to.

`clear` removes storage, not services, and `rm -rf` on the manager runs as
root -- so the question these tests answer is not "does it find the orphans"
but "what does it leave alone". Every case below is a thing that must survive:
a dataset a live experiment still needs, the five directories GraFlag owns, an
image seventeen methods share, a path with `..` in it.

The survey is scripted rather than mocked at the method level, so the parsing
of the remote script's output is under test too -- a field the script stops
emitting shows up here as a wrong verdict, not as a passing test against a
convenient dict.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from graflag.core import GraFlag, GraFlagError  # noqa: E402


class RecordingSSH:
    """An SSHManager double: replays a survey, records everything else."""

    def __init__(self, survey="", responses=None):
        self.survey = survey
        self.responses = responses or {}
        self.commands = []

    def execute(self, command, **kwargs):
        self.commands.append(command)
        for needle, reply in self.responses.items():
            if needle in command:
                return SimpleNamespace(returncode=0, stdout=reply, stderr="")
        if "du -sbL" in command:          # the survey is the only script
            return SimpleNamespace(returncode=0, stdout=self.survey, stderr="")
        if "docker image rm" in command:
            # Stand in for docker's exit status: every image asked for is
            # removed, reported the way the script reports it.
            out = "\n".join(
                "RM " + line.split("docker image rm -f ", 1)[1].split(" >")[0].strip("'")
                for line in command.splitlines() if "docker image rm -f " in line)
            return SimpleNamespace(returncode=0, stdout=out, stderr="")
        return SimpleNamespace(returncode=0, stdout="CLEARED", stderr="")

    @property
    def removals(self):
        return [c for c in self.commands if c.startswith("rm -rf")]


def make(survey, responses=None):
    """A GraFlag wired to the double, with no config file read."""
    gf = GraFlag.__new__(GraFlag)
    gf.config = SimpleNamespace(remote_shared_dir="/shared", manager_ip="10.0.0.1")
    gf.ssh = RecordingSSH(survey, responses)
    gf.docker = None
    return gf


#: A share with two methods, one of them deleted out from under its
#: experiments, and the datasets and images that follow from that.
SURVEY = "\n".join([
    "M taddy",
    "M bond_cola",
    "O bond_cola IMAGE=bond_base",
    "E exp__taddy__uci__20250101_101010",          # method alive
    "E exp__e2e_probe__e2e_data__20250101_101010",  # method gone
    "E exp__e2e_probe__uci__20250102_101010",       # method gone, live dataset
    "E notanexperiment",                            # not GraFlag's naming
    "D uci",
    "D e2e_data",
    "D btc_alpha",                                  # never run: kept
    "S dynwalk",
    "SZ 4096\texperiments/exp__e2e_probe__e2e_data__20250101_101010",
    "SZ 8192\tdatasets/e2e_data",
    "I taddy latest sha256:aaa",
    "I bond_base latest sha256:bbb",
    "I e2e_probe latest sha256:ccc",
    "I 10.0.0.1:5000/e2e_probe latest sha256:fff",
    "I graflag-evaluator ed762eef sha256:ddd",
    "I 10.0.0.1:5000/graflag-evaluator ed762eef sha256:ddd",
    "I nvidia/cuda 11.7.1-runtime-ubuntu22.04 sha256:111",
    "I python 3.9-slim sha256:222",
    "I registry 2 sha256:333",
    "I <none> <none> sha256:eee",
    "IS sha256:ccc 1000",
    "IS sha256:fff 1500",
    "IS sha256:eee 2000",
    "R taddy",
    "R e2e_probe",
    "RC regcontainer",
    "RV registry-data",
    "RSZ 20600000000",
    "AVAIL 31000000000",
])


class WhatSurvives(unittest.TestCase):
    """The safety half: things that must not be collected."""

    def items(self, kind=None, survey=SURVEY):
        report = make(survey).clear(apply=False)
        return {i.name for i in report.items if kind is None or i.kind == kind}

    def test_an_experiment_whose_method_exists_is_kept(self):
        self.assertNotIn("exp__taddy__uci__20250101_101010", self.items())

    def test_a_dataset_a_live_experiment_uses_is_kept(self):
        """The one that makes this command safe to run.

        `uci` is referenced by a dead experiment *and* by a live one. Deciding
        from the dead one alone deletes the dataset every surviving taddy run
        was scored against, and nothing on the share would say why.
        """
        self.assertNotIn("uci", self.items("dataset"))

    def test_a_dataset_nothing_has_run_yet_is_kept(self):
        """"Used by no experiment" is a dataset's state before its first run."""
        self.assertNotIn("btc_alpha", self.items("dataset"))

    def test_a_shared_image_is_kept_for_the_methods_that_declare_it(self):
        """bond_base is owned by IMAGE= in a .env, not by a directory name.

        Reading ownership from directory names alone collects the one image
        seventeen methods share -- and `--build` is the only way back.
        """
        self.assertNotIn("bond_base:latest", self.items("image"))

    def test_the_evaluator_image_is_kept(self):
        """Its tag is a hash of the evaluator's sources, not a method name."""
        self.assertNotIn("graflag_evaluator:abc123", self.items("image"))

    def test_a_directory_not_named_like_an_experiment_is_left_alone(self):
        self.assertNotIn("notanexperiment", self.items())

    def test_a_base_image_is_kept(self):
        """The first dry run of this command proposed deleting these.

        `nvidia/cuda`, `python` and `ubuntu` are owned by no method and are
        what every method Dockerfile builds FROM. A rule that collects
        whatever no method claims removes the base layers of the whole
        cluster and makes the next build re-pull several gigabytes.
        """
        images = self.items("image")
        self.assertNotIn("nvidia/cuda:11.7.1-runtime-ubuntu22.04", images)
        self.assertNotIn("python:3.9-slim", images)
        self.assertNotIn("registry:2", images)

    def test_the_evaluator_is_kept_under_the_name_it_is_actually_built_with(self):
        """It is built as `graflag-evaluator`; the sources say
        `graflag_evaluator`. The first rule tested for the underscore, so it
        matched nothing and proposed deleting both evaluator images."""
        images = self.items("image")
        self.assertNotIn("graflag-evaluator:ed762eef", images)
        self.assertNotIn("10.0.0.1:5000/graflag-evaluator:ed762eef", images)


class WhatTheSurveyReports(unittest.TestCase):
    """The remote script itself, run through a real shell.

    The decision logic above is only as good as the facts it is handed, and
    the facts come from shell. `.dockerignore` sitting at the root of the
    share is the case that matters: without it every build ships 3.6 GB of
    context instead of 804 kB, nothing in GraFlag puts it back, and the first
    version of this survey reported it as a stray to delete.
    """

    def setUp(self):
        import subprocess, tempfile, os
        self.tmp = tempfile.mkdtemp()
        for d in ("methods/taddy", "experiments/exp__taddy__uci__1",
                  "datasets/uci", "leftover_clone", "libs", "images"):
            os.makedirs(os.path.join(self.tmp, d), exist_ok=True)
        for f in (".dockerignore", "nfs_server_ready.txt", "worker1.txt"):
            with open(os.path.join(self.tmp, f), "w"):
                pass
        with open(os.path.join(self.tmp, "methods/taddy/.env"), "w") as fh:
            fh.write("METHOD_NAME=taddy\n")

        # No docker on this machine's PATH for the survey's sake: the share
        # half is what is under test and docker's half must not decide it.
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        fake = os.path.join(bindir, "docker")
        with open(fake, "w") as fh:
            fh.write("#!/bin/sh\nexit 1\n")
        os.chmod(fake, 0o755)

        gf = make(SURVEY)
        gf.clear(apply=False)
        script = gf.ssh.commands[0].replace("SHARED=/shared", f"SHARED={self.tmp}")
        env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
        self.out = subprocess.run(["sh", "-c", script], capture_output=True,
                                  text=True, env=env).stdout
        self.strays = {l[2:] for l in self.out.splitlines() if l.startswith("S ")}

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dockerignore_is_not_a_stray(self):
        self.assertNotIn(".dockerignore", self.strays)

    def test_the_cluster_bootstrap_markers_are_not_strays(self):
        self.assertNotIn("nfs_server_ready.txt", self.strays)
        self.assertNotIn("worker1.txt", self.strays)

    def test_a_leftover_directory_is_still_reported(self):
        """Narrowing strays to directories must not stop finding the thing
        strays exist for -- a pre-refactor copy of a method at the root."""
        self.assertIn("leftover_clone", self.strays)

    def test_the_five_owned_directories_are_never_reported(self):
        for owned in GraFlag.OWNED_DIRS:
            self.assertNotIn(owned, self.strays)

    def test_the_share_facts_are_reported(self):
        self.assertIn("M taddy", self.out)
        self.assertIn("E exp__taddy__uci__1", self.out)
        self.assertIn("D uci", self.out)


class WhatIsCollected(unittest.TestCase):
    """The other half: the orphan really is found."""

    def setUp(self):
        self.report = make(SURVEY).clear(apply=False)
        self.by_kind = {}
        for item in self.report.items:
            self.by_kind.setdefault(item.kind, set()).add(item.name)

    def test_an_experiment_whose_method_is_gone(self):
        self.assertIn("exp__e2e_probe__e2e_data__20250101_101010",
                      self.by_kind["experiment"])

    def test_a_dataset_only_dead_experiments_used(self):
        self.assertEqual(self.by_kind["dataset"], {"e2e_data"})

    def test_an_image_built_for_a_method_that_is_gone(self):
        self.assertIn("e2e_probe:latest", self.by_kind["image"])

    def test_the_registry_copy_of_that_image_too(self):
        """Every build pushes to the cluster registry, so the orphan exists
        twice and freeing only the local one reclaims half of it."""
        self.assertIn("10.0.0.1:5000/e2e_probe:latest", self.by_kind["image"])

    def test_a_dangling_image(self):
        self.assertIn("sha256:eee", self.by_kind["image"])

    def test_a_registry_repository_no_method_owns(self):
        self.assertEqual(self.by_kind["registry"], {"e2e_probe"})

    def test_a_stray_at_the_root_of_the_share(self):
        self.assertIn("dynwalk", self.by_kind["stray"])

    def test_sizes_come_from_the_survey(self):
        experiment = next(i for i in self.report.items if i.kind == "experiment")
        self.assertEqual(experiment.size_bytes, 4096)


class ADryRunRemovesNothing(unittest.TestCase):
    def test_no_rm_is_issued(self):
        gf = make(SURVEY)
        report = gf.clear(apply=False)
        self.assertTrue(report.items)
        self.assertEqual(gf.ssh.removals, [])
        self.assertFalse(any(i.removed for i in report.items))
        self.assertEqual(report.freed_bytes, 0)

    def test_no_image_is_removed(self):
        gf = make(SURVEY)
        gf.clear(apply=False)
        self.assertEqual([c for c in gf.ssh.commands if "image rm" in c], [])

    def test_the_registry_is_never_stopped(self):
        gf = make(SURVEY)
        gf.clear(apply=False, collect=True)
        self.assertEqual([c for c in gf.ssh.commands if "service scale" in c], [])


class ApplyingIt(unittest.TestCase):
    def test_a_hostile_name_cannot_run_a_command_on_the_manager(self):
        """ssh.execute hands the string to the manager's shell (test_ssh.py).

        These names come from a directory listing on a share that is writable
        by every node, not from a CLI argument -- so the `rm -rf` they are
        interpolated into is an injection point. Asserted by running the
        composed command through a real shell and looking for the effect,
        which is what test_ssh.py pins down for every other remote command.
        """
        import subprocess, tempfile, os
        tmp = tempfile.mkdtemp()
        marker = os.path.join(tmp, "executed")
        evil = f"exp__gone__d__1; touch {marker}"
        gf = make(SURVEY + f"\nE {evil}")
        gf.clear(apply=True)
        rm = next(c for c in gf.ssh.removals if "touch" in c)
        # Run it for real, rooted somewhere harmless.
        subprocess.run(["sh", "-c", rm.replace("/shared", tmp)],
                       capture_output=True)
        self.assertFalse(os.path.exists(marker),
                         "an experiment directory name ran as a command")

    def test_the_experiment_path_reaches_the_remove_intact(self):
        gf = make(SURVEY)
        gf.clear(apply=True)
        rm = gf.ssh.removals[0]
        self.assertIn("/shared/experiments/exp__e2e_probe__e2e_data__20250101_101010", rm)

    def test_only_the_decided_paths_are_removed(self):
        gf = make(SURVEY)
        gf.clear(apply=True)
        rm = gf.ssh.removals[0]
        self.assertNotIn("exp__taddy__uci", rm)
        self.assertNotIn("/shared/datasets/uci'", rm)

    def test_freed_bytes_counts_what_was_removed(self):
        report = make(SURVEY).clear(apply=True)
        # experiment 4096 + dataset 8192 + orphan image 1000 + dangling 2000
        # experiment + dataset + both copies of the orphan image + dangling
        self.assertEqual(report.freed_bytes, 4096 + 8192 + 1000 + 1500 + 2000)

    def test_the_registry_repository_is_removed_inside_the_container(self):
        gf = make(SURVEY)
        gf.clear(apply=True)
        self.assertTrue(any(
            "docker exec regcontainer rm -rf" in c
            and "repositories/e2e_probe" in c for c in gf.ssh.commands))


class TheGuard(unittest.TestCase):
    """`rm -rf` runs as root on the manager. This is the last line."""

    def setUp(self):
        self.gf = make(SURVEY)

    def test_traversal_is_refused(self):
        with self.assertRaises(GraFlagError):
            self.gf._clear_guard("/shared/experiments/../../etc")

    def test_a_directory_graflag_owns_is_refused(self):
        for owned in GraFlag.OWNED_DIRS:
            with self.assertRaises(GraFlagError, msg=owned):
                self.gf._clear_guard(f"/shared/{owned}")

    def test_the_share_itself_is_refused(self):
        with self.assertRaises(GraFlagError):
            self.gf._clear_guard("/shared/")

    def test_a_path_outside_the_share_is_refused(self):
        with self.assertRaises(GraFlagError):
            self.gf._clear_guard("/etc/passwd")

    def test_a_child_of_an_owned_directory_is_allowed(self):
        self.assertEqual(
            self.gf._clear_guard("/shared/experiments/exp__x__y__z"),
            "/shared/experiments/exp__x__y__z")

    def test_the_guard_still_blocks_when_the_survey_is_widened(self):
        """Splice: a survey that reports an owned directory as a stray.

        This is the failure the guard exists for -- not a caller passing a bad
        path, but the collection rule upstream growing to include one. The
        loop that builds strays skips these five names; if that skip is ever
        removed, the delete must still not happen.
        """
        widened = SURVEY + "\nS experiments\nS methods"
        gf = make(widened)
        report = gf.clear(apply=True)
        self.assertIn("experiments", {i.name for i in report.items if i.kind == "stray"})
        for rm in gf.ssh.removals:
            self.assertNotIn("'/shared/experiments'", rm)
            self.assertNotIn("'/shared/methods'", rm)
        self.assertTrue(any("GraFlag owns it" in e for e in report.errors))


class CollectingTheRegistry(unittest.TestCase):
    """Garbage collection stops the registry, so it must always restart it."""

    def test_the_registry_comes_back_up(self):
        gf = make(SURVEY)
        gf.clear(apply=True, collect=True)
        scales = [c for c in gf.ssh.commands if "service scale" in c]
        self.assertEqual(scales[0], "docker service scale --detach registry=0")
        self.assertEqual(scales[-1], "docker service scale --detach registry=1")

    def test_a_registry_that_does_not_come_back_is_reported(self):
        """`scale --detach` records the desired state and returns; its exit
        status is 0 whether or not a task ever starts. Reporting on it would
        call a cluster that can no longer pull any image a success."""
        gf = make(SURVEY, responses={"REGISTRY_UP": ""})
        report = gf.clear(apply=True, collect=True)
        self.assertTrue(any("DID NOT COME BACK UP" in e for e in report.errors))

    def test_it_comes_back_up_even_when_collection_raises(self):
        """A GC that dies with the registry at zero replicas takes the
        cluster's ability to pull any image with it."""
        gf = make(SURVEY)

        original = gf.ssh.execute
        def explode(command, **kwargs):
            if "garbage-collect" in command:
                raise RuntimeError("collection died")
            return original(command, **kwargs)
        gf.ssh.execute = explode

        with self.assertRaises(RuntimeError):
            gf.clear(apply=True, collect=True)
        self.assertIn("docker service scale --detach registry=1", gf.ssh.commands)

    def test_it_waits_for_the_registry_to_actually_stop(self):
        """`service scale` returns before the task is gone; collecting against
        a live registry is the documented way to produce an unpullable image."""
        gf = make(SURVEY)
        gf.clear(apply=True, collect=True)
        order = [i for i, c in enumerate(gf.ssh.commands)
                 if "docker ps -q --filter name=registry" in c
                 or "garbage-collect" in c]
        self.assertLess(order[0], order[1])


if __name__ == "__main__":
    unittest.main()
