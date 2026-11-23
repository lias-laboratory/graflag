Quick Start
===========

Requirements
------------

- A client machine with Python 3.8 or newer, OpenSSH and rsync, and SSH
  access to the Swarm manager (as ``root`` unless ``SSH_USER`` says
  otherwise). Linux and macOS work natively. On Windows, run the client
  inside WSL: native Windows has OpenSSH but no rsync, which ``graflag copy``
  and ``graflag sync`` need, and the test suite drives a fake ``ssh`` written
  as a POSIX shell script.
- The manager can sit behind a jump host: set ``SSH_PROXY_JUMP`` (below), or
  write a ``Host`` block for ``MANAGER_IP`` in ``~/.ssh/config``, which
  applies to every connection GraFlag opens.
- A cluster: a manager node and GPU worker nodes sharing an NFS export
  (``/shared`` by default). Without one, the local :doc:`devcluster` provides
  the same setup in Docker containers.

Installation
------------

Install the client from PyPI::

    pip install graflag

Or from source::

    git clone https://github.com/lias-laboratory/graflag.git
    cd graflag
    pip install -e .

Both install the ``graflag`` command.

Shared Storage
--------------

Everything the cluster runs lives in one NFS-exported directory, laid out by the
`graflag-shared <https://github.com/lias-laboratory/graflag-shared>`_
repository. Clone it into the export on the manager::

    cd /shared  # or your NFS export
    git clone https://github.com/lias-laboratory/graflag-shared.git .

It holds:

- ``methods/`` -- one directory per method: a ``.env``, a ``Dockerfile`` (or an
  ``IMAGE=`` key naming a shared one) and usually a ``train_graflag.py``
- ``images/`` -- Dockerfiles shared by several methods; ``bond_base`` serves all
  17 PyGOD detectors
- ``datasets/`` -- one directory per dataset, holding a ``metadata.json``
  descriptor
- ``libs/`` -- the GraFlag libraries: ``graflag_runner``, installed into every
  method image (with ``graflag_bond`` for the PyGOD detectors);
  ``graflag_evaluator``, which runs in its own image; and ``graflag_data``, which
  downloads datasets on the manager
- ``experiments/`` -- one directory per run, created at run time
- ``.dockerignore`` -- keeps ``datasets/`` and ``experiments/`` out of every image
  build. The build context is the whole export, so this file has to stay at its
  root; without it builds still succeed, only several gigabytes larger

Dataset files are not stored in the repository. Each dataset ships only its
``metadata.json``, which names the original source, and the files are
downloaded on first use: ``graflag run`` fetches a missing dataset before it
deploys. To fetch ahead of time, on the manager::

    pip install ./libs/graflag_data
    graflag-data list          # every dataset and whether it is ready
    graflag-data status        # only what is missing; exits non-zero if anything is
    graflag-data fetch uci     # one dataset; omit the name to fetch them all
    graflag-data verify        # re-hash the files against metadata.json

Configuration
-------------

Run the setup wizard::

    graflag setup

It asks for the connection details, stores them in
``~/.config/graflag/config.env``, initialises the Swarm on the manager, joins the
workers listed in ``hosts.yml`` and starts the image registry. The file holds:

.. list-table::
   :header-rows: 1
   :widths: 20 22 58

   * - Key
     - Default
     - Meaning
   * - ``MANAGER_IP``
     - (required)
     - Address of the Swarm manager
   * - ``SSH_PORT``
     - ``22``
     - SSH port on the manager
   * - ``SSH_KEY``
     - ``~/.ssh/id_ed25519``
     - Private key used to reach the manager
   * - ``SSH_USER``
     - ``root``
     - Account on the manager. Another account must be in the ``docker``
       group and able to write ``SHARED_DIR``
   * - ``SSH_PROXY_JUMP``
     - (none)
     - Jump host for every connection, as ssh's ``-J``/``ProxyJump`` takes it
       (``user@bastion``, or a ``Host`` alias)
   * - ``SHARED_DIR``
     - ``/shared``
     - The NFS export, mounted at the same path on every node
   * - ``HOSTS_FILE``
     - ``hosts.yml``
     - Worker IPs; read by ``graflag setup`` and ``graflag devcluster`` only
   * - ``NFS_PORT``
     - ``2049``
     - NFS port used to mount the export
   * - ``GRAFLAG_LIBS``
     - ``local``
     - Where method images get ``graflag_runner`` and ``graflag_bond``:
       ``local`` installs the copy in ``SHARED_DIR/libs``, ``pypi`` the
       published release

The wizard asks for ``MANAGER_IP``, ``SSH_PORT``, ``SSH_KEY``, ``SHARED_DIR``
and ``HOSTS_FILE``; set the others by editing the file.
``graflag setup --reconfigure`` runs the wizard again. A manager GraFlag
cannot reach is an error naming the account, the jump host and ssh's own
message, never an empty listing.

GraFlag takes its configuration from, in order: the file named by ``--config``;
a ``.env`` in the working directory, but only if it defines ``MANAGER_IP``, so
an unrelated project's ``.env`` is never mistaken for one; and
``~/.config/graflag/config.env``.

Clusters Behind an HTTP Proxy
-----------------------------

GraFlag itself only needs SSH to the manager. Everything that reaches the
internet runs on the nodes -- image builds on the manager, base-image pulls on
every node, dataset downloads on the manager -- so on a network where only a
proxy gets out, each of those needs the proxy, and the cluster's own traffic
must bypass it. On every node:

- **Image pulls** (``docker pull``, Swarm tasks): a systemd drop-in for the
  daemon, e.g. ``/etc/systemd/system/docker.service.d/proxy.conf``::

      [Service]
      Environment="HTTP_PROXY=http://proxy:3128" "HTTPS_PROXY=http://proxy:3128"
      Environment="NO_PROXY=localhost,127.0.0.1,MANAGER_IP,CLUSTER_SUBNET"

- **Build steps** (``pip install``, ``git clone`` inside a ``RUN``): the
  client's ``~/.docker/config.json`` for ``root`` on the manager, whose
  ``proxies.default`` (``httpProxy``, ``httpsProxy``, ``noProxy``) Docker
  passes to every build as build arguments.
- **Dataset downloads** (``graflag_data`` over SSH): ``http_proxy``,
  ``https_proxy`` and ``no_proxy`` in ``/etc/environment``, which SSH sessions
  read.

``NO_PROXY`` must cover the manager's registry (``MANAGER_IP:5000``) and the
cluster subnet, or workers try to pull images through the proxy. Method
containers get no proxy: a method that downloads at run time fails there
rather than silently using data GraFlag did not mount.

Running an Experiment
---------------------

Build a method's image and run it on a dataset::

    graflag run -m bond_dominant -d bond_inj_cora --build

``run`` checks that the method and the dataset exist, fetches the dataset if
its files are missing, builds and pushes the image (with ``--build``), deploys a
one-shot Swarm service and follows its output until the run ends. Ctrl-C only
detaches from the output: the run continues, and ``graflag logs -e EXP -f``
reattaches. When the run ends, its service is removed, unless ``--keep-service``
is given.

Without ``--build``, the image already in the registry runs, so rebuild after
editing a method. Parameters are overridden without the underscore their
``.env`` declares them with::

    graflag run -m taddy -d uci --params MAX_EPOCH=100 LEARNING_RATE=0.001

Every run saves its configuration in its directory on the cluster, as
``service_config.json``. To replay a run, copy that file to the client and pass
it to ``run``::

    graflag copy --from-remote -s experiments/EXP/service_config.json --dest .
    graflag run --from-config service_config.json

The method, the dataset, the parameters, the image tag and whether the run had
a GPU all come from the file; options given on the command line override them.

Monitoring
----------

::

    graflag status                  # nodes, services and the shared directory
    graflag list experiments        # every run and its status
    graflag logs -e EXP -f          # follow a run's output
    graflag stop -e EXP             # stop a run; --rm also deletes its directory

Evaluating Results
------------------

::

    graflag evaluate -e exp__bond_dominant__bond_inj_cora__20260923_151827

This deploys a short-lived evaluator service. It computes AUC-ROC, AUC-PR,
precision, recall and F1 at *k*, and the best F1, and writes ``evaluation.json``
and the plots to the experiment's ``eval/`` directory. :doc:`RESULTS_STANDARD`
describes what the evaluator expects and which scores it leaves out.

Then check that the number means what it says::

    graflag verify -e exp__bond_dominant__bond_inj_cora__20260923_151827

``verify`` compares what the method reported about itself with what it
published -- among other checks, that the AUC the evaluator just computed is
the AUC the method printed -- and exits 1 when a check fails. See
:ref:`the CLI reference <cli-verify>`.

Custom Metrics
~~~~~~~~~~~~~~

Register a custom metric from the Python API::

    from graflag import GraFlag

    def hits_at_100(scores, ground_truth, **kw):
        top = scores.argsort()[-100:]
        return {"hits@100": float(ground_truth[top].mean())}

    gf = GraFlag()
    # Globally: applied to every later evaluation of this result type
    gf.register_metric("EDGE_STREAM_ANOMALY_SCORES", hits_at_100)

    # Or for one experiment only
    gf.register_metric("EDGE_STREAM_ANOMALY_SCORES", hits_at_100,
                       experiment="exp__taddy__uci__20260309_120000")

``register_metric`` takes the function's source and writes it as a plugin file
on the cluster, which the evaluator loads before computing metrics. Plugin files
can also be written by hand, in either location:

- ``libs/graflag_evaluator/plugins/`` -- global
- ``experiments/<exp_name>/custom_metrics/`` -- one experiment

Each plugin registers its function when imported::

    # custom_metrics/hits_at_100.py
    from graflag_evaluator import MetricCalculator

    def hits_at_100(scores, ground_truth, **kw):
        top = scores.argsort()[-100:]
        return {"hits@100": float(ground_truth[top].mean())}

    MetricCalculator.register_metric("EDGE_STREAM_ANOMALY_SCORES", hits_at_100)

Cleaning Up
-----------

A run removes its own service when it ends, and ``graflag stop`` removes the
service of the run it stops. ``graflag cleanup`` removes the ones left behind
otherwise: by a run you detached from with Ctrl-C, or one started with
``--keep-service``. It touches finished runs only, and keeps a service whenever
the run could not be diagnosed without it::

    graflag cleanup --dry-run       # what would be removed
    graflag cleanup

``graflag clear`` reclaims storage: experiments whose method no longer exists,
datasets used only by them, images no method owns, and stray files at the root
of the share. It only reports unless given ``--apply``::

    graflag clear                   # report what is reclaimable
    graflag clear --apply --images  # remove images and registry repositories only
    graflag clear --apply --gc      # also garbage-collect the registry's blobs

Deleting a registry repository frees no disk by itself; ``--gc`` does, and stops
the registry while it runs, because collecting blobs from a live registry
corrupts later pushes.

Web Dashboard
-------------

::

    graflag gui

Then open ``http://localhost:5000``. The dashboard has no authentication: read
:doc:`gui` before running it anywhere but a trusted network.

From an AI Agent
----------------

GraFlag also runs as an MCP server, so an agent such as Claude Code can list
methods and datasets, launch and watch runs, evaluate and verify them::

    pip install "graflag[mcp]"            # Python 3.10+
    claude mcp add graflag -- graflag mcp

:doc:`MCP` lists the tools and what they deliberately leave out.
