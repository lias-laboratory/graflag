CLI Reference
=============

``graflag`` is a thin client: every command acts on the Swarm manager over SSH,
except ``devcluster``, which runs the development cluster on this machine. The
command comes first, then its options::

    graflag COMMAND [OPTIONS]

Two options apply to every command:

- ``-c, --config FILE`` -- use this configuration file (see :doc:`quickstart`
  for the default order)
- ``-v, --verbose`` -- debug logging, and a traceback on unexpected errors

Cluster
-------

setup
~~~~~

Configure the client and prepare the cluster::

    graflag setup [--reconfigure]

The first run asks for the connection details and stores them in
``~/.config/graflag/config.env``. Every run then initialises the Swarm on the
manager, joins the workers listed in ``hosts.yml``, starts the image registry,
and prints the resulting status.

- ``--reconfigure`` -- ask for the connection details again

status
~~~~~~

Show the nodes, the running services and the contents of the shared
directory::

    graflag status

Experiments
-----------

run
~~~

Run a method on a dataset::

    graflag run -m METHOD -d DATASET [--build [--force-rm]] [--no-gpu]
                [--params KEY=VALUE ...] [--tag TAG] [--keep-service]
    graflag run --from-config service_config.json [--params KEY=VALUE ...]

``run`` is blocking: after deploying the service it follows the method's output
until the run ends. Ctrl-C detaches and leaves the run going.

- ``-m, --method`` -- method name
- ``-d, --dataset`` -- dataset name
- ``-b, --build`` -- build the image and push it to the registry first.
  Without it, the image already in the registry runs
- ``--force-rm`` -- with ``--build``, remove the build's intermediate containers
  even if the build fails. Without it a failed build leaves its last container,
  and every layer under it, on the manager
- ``--no-gpu`` -- schedule without reserving a GPU; a method that declares
  ``_GPU`` gets ``-1``, which means CPU
- ``-p, --params KEY=VALUE ...`` -- override parameters, named without the
  leading underscore their ``.env`` uses
- ``-t, --tag`` -- image tag (default: ``latest``, or the recorded one with
  ``--from-config``)
- ``--keep-service`` -- leave the finished Swarm service in place, for
  ``docker service ps``; by default it is removed when the run ends
- ``--from-config FILE`` -- replay a run from its ``service_config.json``, copied
  from the experiment's directory with ``graflag copy --from-remote``. The
  method, the dataset, the parameters, the image tag and the GPU choice come
  from the file; options given on the command line override them

logs
~~~~

Show a run's output::

    graflag logs -e EXPERIMENT [-f] [--tee FILE]

- ``-f, --follow`` -- stream until the run ends
- ``--tee FILE`` -- also write the output to a file

Once the service is gone, the output comes from the experiment's
``method_output.txt``.

stop
~~~~

Stop a running experiment and remove its service::

    graflag stop -e EXPERIMENT [--rm]

- ``--rm`` -- also delete the experiment directory

evaluate
~~~~~~~~

Compute metrics and plots for a finished run::

    graflag evaluate -e EXPERIMENT

The results go to the experiment's ``eval/`` directory.

list
~~~~

::

    graflag list methods
    graflag list datasets
    graflag list experiments
    graflag list services

Housekeeping
------------

cleanup
~~~~~~~

Remove the Swarm services of finished runs::

    graflag cleanup [--dry-run] [-e EXPERIMENT]

Only finished runs are touched, and a service is kept when the run could not be
diagnosed without it (no ``method_output.txt``, and no error recorded in
``status.json``).

- ``--dry-run`` -- report what would be removed
- ``-e`` -- only this experiment

clear
~~~~~

Reclaim storage that nothing owns any more: experiments whose method is gone,
datasets used only by those experiments, images and registry repositories no
method owns, and stray files at the root of the share::

    graflag clear [--apply] [--share] [--images] [--gc]

Without ``--apply`` it only reports; the report and the applied run list the
same items.

- ``--apply`` -- remove what is reported
- ``--share`` -- limit the sweep to the shared directory
- ``--images`` -- limit the sweep to images and registry repositories. Neither
  flag, or both, sweeps everything
- ``--gc`` -- with ``--apply``, also garbage-collect the registry's blobs.
  Deleting a repository alone frees no disk. The registry is stopped for the
  duration, because collecting from a live registry makes later pushes skip
  layers that no longer exist

Files
-----

copy
~~~~

Copy files between the client and the shared directory (rsync over SSH)::

    graflag copy -s SOURCE [SOURCE ...] --dest DEST [--from-remote]

rsync semantics apply: ``dir`` copies the directory itself into ``DEST``,
``dir/`` copies its contents. Remote paths are relative to the shared
directory. ``--dest`` has no short form, since ``-d`` is ``--dataset``.

- ``--from-remote`` -- copy from the cluster to the client
- ``-r`` -- accepted for compatibility; directories are always copied
  recursively

sync
~~~~

Copy a method or a library directory to the shared directory::

    graflag sync [--path DIR] [--lib]

A method directory (the working directory by default) goes to
``methods/<METHOD_NAME>``, as named in its ``.env``; with ``--lib`` the
directory goes to ``libs/<name>``. ``sync`` adds and overwrites but never
deletes: remove a file on the share by hand when you remove it locally. A synced
library reaches a method only when its image is rebuilt with ``--build``.

Interfaces
----------

gui
~~~

Start the web dashboard::

    graflag gui [--host HOST] [--port PORT] [--debug]

- ``--host`` -- address to bind (default: ``0.0.0.0``)
- ``--port`` -- port (default: ``5000``)
- ``--debug`` -- Flask debug mode

The dashboard has no authentication. See :doc:`gui`.

devcluster
~~~~~~~~~~

Deploy or remove the local development cluster::

    graflag devcluster --hosts hosts.yml [--pubkey ~/.ssh/id_ed25519.pub]
    graflag devcluster --down

- ``--hosts`` -- the ``hosts.yml`` describing the cluster (required to deploy)
- ``--pubkey`` -- public key the nodes will trust (default:
  ``~/.ssh/id_ed25519.pub``)
- ``--down`` -- stop and remove the cluster and its volumes

See :doc:`devcluster`.

graflag-data
------------

A separate command, installed with ``pip install ./libs/graflag_data`` on the
machine that holds the shared directory. It downloads dataset files from their
original sources::

    graflag-data list              # every dataset and whether it is ready
    graflag-data status            # what is missing; exits non-zero if anything is
    graflag-data fetch [DATASET] [--force]
    graflag-data verify            # re-hash files against metadata.json

``graflag run`` fetches its own dataset, so this is only needed to fetch ahead
of time.
