Demo Video
==========

A narrated walkthrough, in under four minutes: the command line, a method
integration, a GPU run, the web dashboard, and a replayed experiment.

.. raw:: html

   <video id="demo-player" controls preload="metadata" width="100%"
          poster="_static/graflag_demo_poster.jpg"
          style="border-radius:8px; margin:16px 0; box-shadow:0 4px 12px rgba(0,0,0,0.15);">
     <source src="_static/graflag_demo.mp4" type="video/mp4">
     <track kind="captions" src="_static/graflag_demo.vtt" srclang="en" label="English">
     Your browser does not support the video tag.
   </video>

Chapters
--------

Select a time to jump to that chapter.

.. raw:: html

   <style>
     .wy-table-responsive table.demo-chapters td { white-space: normal; vertical-align: top; }
     .wy-table-responsive table.demo-chapters code { white-space: normal; }
     @media (max-width: 480px) {
       .wy-table-responsive table.demo-chapters td,
       .wy-table-responsive table.demo-chapters th { padding: 6px 8px; }
     }
   </style>
   <table class="docutils align-default demo-chapters">
     <thead><tr><th>Time</th><th>Chapter</th><th>What it shows</th></tr></thead>
     <tbody>
       <tr><td><a href="#demo-player" data-seek="0">0:00</a></td><td>Introduction</td>
           <td>What GraFlag is for.</td></tr>
       <tr><td><a href="#demo-player" data-seek="14">0:14</a></td><td>How it works</td>
           <td>The client, the Docker Swarm cluster, one service per experiment, and the shared NFS store.</td></tr>
       <tr><td><a href="#demo-player" data-seek="31">0:31</a></td><td>Command line</td>
           <td><code>graflag status</code>, then the method and dataset catalogues.</td></tr>
       <tr><td><a href="#demo-player" data-seek="54">0:54</a></td><td>Integrating a method</td>
           <td>The files that make up RARE's integration, its <code>.env</code> pinned to the authors' commit, and the contract tests every method definition passes.</td></tr>
       <tr><td><a href="#demo-player" data-seek="78">1:18</a></td><td>Running a method</td>
           <td><code>graflag run</code> on a GPU worker: the method's own AUROC, the runner's time and memory measurements, the service removed, then <code>graflag evaluate</code>.</td></tr>
       <tr><td><a href="#demo-player" data-seek="124">2:04</a></td><td>Web dashboard</td>
           <td>Launching AnoGraph on ISCX, evaluating it, and opening its plots.</td></tr>
       <tr><td><a href="#demo-player" data-seek="178">2:58</a></td><td>Reproducing a run</td>
           <td>Replaying a saved <code>service_config.json</code> on another worker, then evaluating the replay: the same AUC-ROC as the original.</td></tr>
       <tr><td><a href="#demo-player" data-seek="210">3:30</a></td><td>Getting started</td>
           <td><code>pip install graflag</code>.</td></tr>
     </tbody>
   </table>
   <script>
     document.querySelectorAll('a[data-seek]').forEach(function (a) {
       a.addEventListener('click', function (e) {
         e.preventDefault();
         var v = document.getElementById('demo-player');
         v.currentTime = Number(a.dataset.seek);
         v.scrollIntoView({ behavior: 'smooth', block: 'center' });
         v.play();
       });
     });
   </script>

About the recording
-------------------

The video was recorded on the development cluster that :doc:`graflag devcluster <devcluster>`
deploys: a manager and four workers, running as Docker containers on one workstation
and sharing its GPU, an NVIDIA RTX A2000 (12 GB). Swarm schedules them as five
separate nodes, so the worker names in the logs are the ones a physical cluster
would show.

Every command in the terminal scenes was really run, and the output on screen is
exactly what it printed. Long pauses are shortened, and the one sped-up stretch,
RARE's search phase, is labelled on screen. The dashboard scene plays in real time.

Captions are available from the player's captions menu.
