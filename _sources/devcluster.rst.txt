Development Cluster
===================

The ``graflag devcluster`` command deploys a Docker Compose-based virtual cluster
for local development and testing without a physical multi-node setup.

Usage
-----

::

    graflag devcluster --hosts hosts.yml [--pubkey ~/.ssh/id_ed25519.pub]

Setup
-----

1. Create a ``hosts.yml`` with your desired network configuration::

    subnet: 192.168.100.0/24
    manager: 192.168.100.10
    workers:
        - 192.168.100.11
        - 192.168.100.12
        - 192.168.100.13
        - 192.168.100.14

2. Deploy the cluster::

    graflag devcluster --hosts hosts.yml

3. Run the setup wizard (it will ask for the ``hosts.yml`` path)::

    graflag setup

   The wizard stores the hosts file path in ``~/.config/graflag/config.env``
   so worker IPs are available for Swarm initialization.

Architecture
------------

The deploy script creates Docker containers running Docker-in-Docker:

- **Manager**: Docker daemon, NFS server exporting the shared directory, SSH server
- **Workers**: Docker daemon, NFS client mount, SSH server

The image registry is not part of the deployment: ``graflag setup`` (step 3)
starts it on the manager, and every daemon is configured to trust it.

All containers join a bridge network with static IPs from ``hosts.yml``.
GPU resources are passed through via NVIDIA Container Toolkit.

The cluster mirrors a real multi-node setup, allowing full testing of
the benchmarking pipeline locally.

Where it keeps its state
------------------------

``deploy.sh`` generates ``docker-compose.yml``, copies in your ``hosts.yml``
and writes the SSH keys. These go to ``$XDG_STATE_HOME/graflag/devcluster``
(``~/.local/state/graflag/devcluster`` by default), not into the installed
package -- writing there would fail on a root-owned or read-only install and
would dirty an editable checkout. ``--down`` reads the compose file from the
same place, falling back to the package directory so a cluster deployed by an
older version can still be torn down.

Teardown
--------

Stop and remove the cluster, including its volumes::

    graflag devcluster --down

The containers are ``privileged`` (Docker-in-Docker requires it) and each one
reserves the host's GPUs, so leave them running only while you need them.

Notes
-----

**The shared directory lives in memory.** ``/shared`` on the manager links to
``/tmp/shared``, and Docker-in-Docker mounts ``/tmp`` as a tmpfs. Datasets and
experiments are lost whenever the manager container stops -- ``--down``
included -- and the directory can hold no more than the tmpfs allows, a share
of the host's memory. Copy off what you want to keep with
``graflag copy --from-remote`` first.

**Re-deploying reuses the same IPs.** The manager's SSH host key is created when
its image is built, so a redeployment that rebuilds the image from scratch -- or
a cluster on another machine at the same addresses -- presents a new key that
will not match the entries your client already has. GraFlag connects with
``StrictHostKeyChecking=no`` and is unaffected, but plain ``ssh`` to the manager
will complain until you drop the old entry::

    ssh-keygen -R 192.168.100.10

**The pubkey you deploy with must match ``SSH_KEY`` in your config.** The
cluster only trusts the key passed to ``--pubkey`` (default
``~/.ssh/id_ed25519.pub``). If ``graflag status`` reports
``Permission denied (publickey)``, the two have diverged -- check which config
file is actually in use, since a ``.env`` in the working directory takes
precedence over ``~/.config/graflag/config.env``.

**Run one experiment at a time.** Every node of a devcluster shares the
host's memory and GPU, and memory is what runs out first: two concurrent runs
can be killed by the kernel, and the method's log then simply ends with
``Killed``. ``dmesg -T | grep -i oom-kill`` on the host confirms it.

**Worker setup needs the hosts file.** ``graflag setup`` reads ``HOSTS_FILE``
from the config to find the worker IPs. If it is missing or unreadable, setup
warns and the swarm ends up with the manager only -- check ``graflag status``
lists every node before running experiments.
