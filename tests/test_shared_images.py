"""Which image a method builds, and whether the run pulls that same one.

Seventeen `bond_*` methods have byte-identical Dockerfiles. Building one image
each meant seventeen copies of the same ~9 GB layer stack, which is why
building the whole method tree did not fit on the cluster at all -- so
`IMAGE=bond_base` in the `.env` is a precondition for running the benchmark,
not housekeeping.

It introduces a way to fail that did not exist before: build one image name and
deploy another. That failure is invisible until Swarm reports an image it
cannot pull, minutes after the build logged `[OK]`. The name used to be built
from an f-string in `build_method_image` and from an identical one in
`create_service`; identical is exactly the problem, since the two are written
apart and only one gets edited. These tests read the name off both paths and
compare them, so the day they disagree is the day a test fails rather than the
day a run does.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from graflag.docker_ops import DockerManager  # noqa: E402


BOND_ENV = (
    "METHOD_NAME=bond_cola\n"
    "SOURCE_CODE=https://docs.pygod.org/en/latest/generated/"
    "pygod.detector.CoLA.html#pygod.detector.CoLA\n"
    "INTEGRATION=upstream\n"
    "IMAGE=bond_base\n"
    "COMMAND=python3 -m graflag_bond.train\n"
    "_GPU=0\n"
)

OWN_ENV = (
    "METHOD_NAME=gady\n"
    "SOURCE_CODE=https://github.com/mufeng-74/GADY\n"
    "SOURCE_REF=1e8e4503e238f0a6c093c3aba946085aa3959410\n"
    "INTEGRATION=upstream\n"
    "COMMAND=python3 train_graflag.py\n"
    "_GPU=0\n"
)


class RecordingSSH:
    """An SSHManager double: records commands, answers the .env reads."""

    def __init__(self, env_text):
        self.env_text = env_text
        self.commands = []

    def execute(self, command, **kwargs):
        self.commands.append(command)
        if command.startswith("test -f"):
            out = "exists"
        elif command.startswith("cat "):
            out = self.env_text
        else:
            out = ""
        return SimpleNamespace(returncode=0, stdout=out, stderr="")


class ImageResolution(unittest.TestCase):
    """The two halves, driven separately and compared."""

    def manager(self, env_text):
        ssh = RecordingSSH(env_text)
        config = SimpleNamespace(remote_shared_dir="/shared", manager_ip="10.0.0.1")
        mgr = DockerManager(ssh, config)
        # Bypass the SSH tunnel: the client is only needed so create_service
        # can hand the image name to services.create, which is what is read.
        # The created service answers with a plausible shape so the details
        # writer succeeds quietly instead of logging a warning per test.
        mgr._client = mock.Mock()
        mgr._client.services.create.return_value = SimpleNamespace(
            id="svc", name="svc", attrs={}, tasks=lambda: [])
        return mgr, ssh

    def built(self, env_text, method):
        """The image `docker build` tags, read off the composed command."""
        mgr, ssh = self.manager(env_text)
        mgr.build_method_image(method)
        cmd = next(c for c in ssh.commands if c.startswith("docker build"))
        # `-t local -t registry`, in that order.
        tags = [part for part in cmd.split() if part]
        found = [tags[i + 1] for i, p in enumerate(tags) if p == "-t"]
        return SimpleNamespace(tags=found, command=cmd)

    def deployed(self, env_text, method):
        """The image Swarm is asked to pull, read off services.create."""
        mgr, ssh = self.manager(env_text)
        mgr.create_service(f"exp__{method}__d__20260101_000000", method, "d")
        return mgr._client.services.create.call_args.kwargs["image"]

    # -- the seam ---------------------------------------------------------

    def test_a_shared_image_is_built_and_deployed_under_one_name(self):
        """The regression the resolver exists to prevent.

        Splice either f-string back and this is what goes red: the build
        produces `bond_base:latest` while the service asks the registry for
        `bond_cola:latest`, which was never pushed.
        """
        built = self.built(BOND_ENV, "bond_cola")
        self.assertEqual(self.deployed(BOND_ENV, "bond_cola"), built.tags[1])
        self.assertEqual(built.tags, ["bond_base:latest",
                                      "10.0.0.1:5000/bond_base:latest"])

    def test_a_method_without_a_shared_image_is_still_named_after_itself(self):
        """The common case must not be disturbed by the uncommon one."""
        built = self.built(OWN_ENV, "gady")
        self.assertEqual(built.tags, ["gady:latest", "10.0.0.1:5000/gady:latest"])
        self.assertEqual(self.deployed(OWN_ENV, "gady"),
                         "10.0.0.1:5000/gady:latest")

    def test_every_bond_method_resolves_to_the_same_image(self):
        """One image for seventeen methods is the whole point.

        If any of them resolved to its own name the disk saving would quietly
        be partial, and the cluster would run out of space in the middle of
        the benchmark rather than before it.
        """
        names = set()
        for method in ("bond_cola", "bond_dominant", "bond_adone", "bond_card"):
            env = BOND_ENV.replace("METHOD_NAME=bond_cola",
                                   f"METHOD_NAME={method}")
            names.add(self.deployed(env, method))
        self.assertEqual(names, {"10.0.0.1:5000/bond_base:latest"})

    # -- the Dockerfile that backs the name --------------------------------

    def test_a_shared_image_builds_from_the_images_directory(self):
        """Not methods/bond_cola/Dockerfile: there is no longer one there."""
        self.assertIn("-f /shared/images/bond_base/Dockerfile",
                      self.built(BOND_ENV, "bond_cola").command)

    def test_a_method_of_its_own_builds_from_its_method_directory(self):
        self.assertIn("-f /shared/methods/gady/Dockerfile",
                      self.built(OWN_ENV, "gady").command)

    def test_the_build_context_is_still_the_share(self):
        """COPY paths in a shared Dockerfile read `images/...` because of this."""
        for env, method in ((BOND_ENV, "bond_cola"), (OWN_ENV, "gady")):
            with self.subTest(method=method):
                self.assertTrue(self.built(env, method).command.rstrip().endswith("/shared/"))

    # -- what a bad value does ---------------------------------------------

    def test_an_unusable_image_name_is_refused_rather_than_ignored(self):
        """Falling back to the method's own name would be the fail-open shape.

        It would build `bond_cola:latest` from a Dockerfile that is not there,
        or -- worse, if one were -- build and deploy successfully under a name
        nobody asked for, and report success either way. Docker rejects an
        uppercase repository at push time, minutes in; this rejects it before
        the build starts and says which method declared it.
        """
        for bad in ("Bond Base", "-leading-dash", "bond/base", "bond:base", "../etc"):
            env = BOND_ENV.replace("IMAGE=bond_base", f"IMAGE={bad}")
            with self.subTest(image=bad):
                mgr, _ = self.manager(env)
                with self.assertRaises(ValueError) as caught:
                    mgr._image_names("bond_cola")
                self.assertIn("bond_cola", str(caught.exception))

    def test_an_uppercase_image_name_is_lowercased_like_a_method_name(self):
        """`graflag run -m BOND_Cola` already works; IMAGE= behaves the same.

        Docker repositories are lowercase, and the method name has been
        lowercased on both paths for the same reason, so a capitalised IMAGE=
        is a spelling of the same image rather than a different one.
        """
        env = BOND_ENV.replace("IMAGE=bond_base", "IMAGE=Bond_Base")
        self.assertEqual(self.deployed(env, "bond_cola"),
                         "10.0.0.1:5000/bond_base:latest")

    # -- what sharing must not change --------------------------------------

    def test_sharing_an_image_does_not_merge_the_methods(self):
        """METHOD_NAME is what keeps seventeen methods distinct at run time.

        graflag_bond.train reads it to pick the PyGOD detector, so a service
        that inherited the image's name instead would run CoLA seventeen times
        and file the results under seventeen names.
        """
        mgr, _ = self.manager(BOND_ENV)
        mgr.create_service("exp__bond_dominant__d__20260101_000000",
                           "bond_dominant", "d")
        env_vars = mgr._client.services.create.call_args.kwargs["env"]
        self.assertIn("METHOD_NAME=bond_dominant", env_vars)
        self.assertNotIn("METHOD_NAME=bond_base", env_vars)

    def test_the_recorded_config_names_the_image_that_ran(self):
        """`run --from-config` replays the shared image, not a method-named one."""
        mgr, ssh = self.manager(BOND_ENV)
        mgr.create_service("exp__bond_cola__d__20260101_000000", "bond_cola", "d")
        written = next(c for c in ssh.commands if "service_config.json" in c)
        self.assertIn("10.0.0.1:5000/bond_base:latest", written)

    def test_creating_a_service_reads_the_env_once(self):
        """Resolving the image must not cost a second remote read per service.

        The image name and the service environment both come out of the same
        `.env`; reading it twice is the "one SSH call, not one per item" rule
        that list_experiments() follows, broken in a place nobody would look.
        """
        mgr, ssh = self.manager(BOND_ENV)
        mgr.create_service("exp__bond_cola__d__20260101_000000", "bond_cola", "d")
        reads = [c for c in ssh.commands if c.startswith("cat /shared/methods/")]
        self.assertEqual(len(reads), 1, reads)


if __name__ == "__main__":
    unittest.main()
