Web Dashboard
=============

The ``graflag gui`` command starts a web dashboard for monitoring and managing experiments.

Usage
-----

::

    graflag gui [--host HOST] [--port PORT] [--debug] [--config FILE]

Default: ``http://0.0.0.0:5000``

.. warning::

   ``0.0.0.0`` binds every interface, so the dashboard is reachable from any
   host that can route to the machine. The dashboard has **no authentication**,
   and every action it exposes -- running experiments, deleting experiment
   directories, reading files -- is carried out on the swarm manager as
   ``root``. Run it on a trusted network, or bind it explicitly::

       graflag gui --host 127.0.0.1

   Experiment, method and dataset names arriving over HTTP are validated
   against ``^[A-Za-z0-9._-]{1,200}$`` before they reach any remote command,
   but that guard is not a substitute for network isolation.

Features
--------

- **Cluster status** -- connection and Swarm health in the navbar
- **Methods and datasets** -- browse available resources with pagination
- **Run experiments** -- select method, dataset, parameters, and deploy
- **Live updates** -- the experiment and service lists are pushed over a
  WebSocket (``update`` events) as the background poller notices changes
- **Logs** -- fetched over HTTP from ``/api/experiments/<name>/logs?tail=200``:
  every two seconds while the output keeps changing, backing off to thirty
  seconds when it goes quiet, and not at all once the run has finished. They
  are polled, not streamed, and only the last 200 lines are shown (the last
  1000 in the Run form's live view); use ``graflag logs -e EXP -f`` for a true
  stream
- **Evaluation viewer** -- the scalar metrics (counts as integers, with the
  number of samples the evaluator excluded when there are any) and every plot
  in a grid, each opening full size in a lightbox
- **Notifications** -- browser notifications for experiment status changes
- **Dark/light theme**

API Endpoints
-------------

.. list-table::
   :header-rows: 1

   * - Method
     - Endpoint
     - Description
   * - GET
     - /api/cluster/info
     - Cluster status
   * - GET
     - /api/methods
     - List methods
   * - GET
     - /api/datasets
     - List datasets
   * - GET
     - /api/experiments
     - List experiments
   * - GET
     - /api/experiments/<name>/results
     - Experiment results
   * - GET
     - /api/experiments/<name>/evaluation
     - Evaluation metrics
   * - GET
     - /api/experiments/<name>
     - Experiment details
   * - GET
     - /api/experiments/<name>/logs
     - Experiment logs (last ``tail`` lines, default 100)
   * - GET
     - /api/experiments/<name>/plot/<plot_name>
     - Evaluation plot image
   * - GET
     - /api/methods/<method_name>
     - Method details
   * - GET
     - /api/services
     - Running services
   * - POST
     - /api/run
     - Run experiment
   * - POST
     - /api/experiments/<name>/evaluate
     - Evaluate experiment
   * - POST
     - /api/experiments/<name>/stop
     - Stop experiment
   * - POST
     - /api/experiments/<name>/delete
     - Delete experiment
