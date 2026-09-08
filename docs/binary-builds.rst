Binary Builds
=============

Official binary releases target:

- ``darwin-amd64``
- ``darwin-arm64``
- ``linux-arm64``
- ``linux-amd64``

Local maintainer builds
-----------------------

Build from the repository root:

.. code-block:: bash

   bash scripts/build.sh macos
   bash scripts/build.sh linux
   bash scripts/build.sh all

The script writes platform binaries plus ``.sha256`` files into ``dist/`` and
runs basic smoke tests on the host-supported artifacts.

Release flow
------------

The manual GitHub Actions release workflow now builds:

- Python sdist/wheel outputs
- ``lium-darwin-amd64``
- ``lium-darwin-arm64``
- ``lium-linux-arm64``
- ``lium-linux-amd64``
- ``install.sh``
- ``checksums.txt``

Binary assets are uploaded to GitHub Releases so the public installer can fetch
``releases/latest/download/<asset>`` without relying on private infrastructure.
Fresh installs keep ``~/.lium/bin/lium`` on ``PATH`` as a symlink to the managed
versioned binary stored in ``~/.lium/versions/<version>/lium``.

What the Linux bundle ships
---------------------------

PyInstaller copies the build image's ``libssl.so.1.1``, ``libcrypto.so.1.1`` and
``libpython3.12.so.1.0`` into ``dist/lium/_internal``. After every Linux build,
``ci.yml`` and ``release.yml`` run ``scripts/linux_bundle_report.py``, which writes
to the run's step summary the Debian ``libssl1.1`` package version,
``ssl.OPENSSL_VERSION``, the Python version and the bundled ``requests``,
``paramiko`` and ``cryptography`` versions. They are read inside the build image;
the OpenSSL and Python values are tied to the bundle by the sha256 of those three
libraries, the wheel versions are the build venv's. The step fails when
``libssl1.1`` is below ``1.1.1w-0+deb11u8`` (the version ``Dockerfile.build`` pins)
or a bundled library is not the image's file; ``release-assets`` needs the build
job, so such a bundle cannot be published. ``ssl.OPENSSL_VERSION`` alone cannot
tell ``deb11u3`` from ``deb11u8`` (both print ``OpenSSL 1.1.1w  11 Sep 2023``),
which is why the check reads the Debian package version.

Locally, after ``docker build -f Dockerfile.build -t lium-build:local .`` and
copying ``/app/dist/lium`` out of the image to ``dist/lium``:

.. code-block:: bash

   python3 scripts/linux_bundle_report.py --bundle dist/lium --image lium-build:local

Binary runtime notes
--------------------

- The frozen entrypoint uses ``multiprocessing.freeze_support()`` to avoid
  child-process argument parsing issues under PyInstaller.
- The CLI version falls back to in-repo version metadata when distribution
  metadata is unavailable in a frozen build.
- ``lium/cli/themes.json`` is bundled into the PyInstaller build and loaded from
  the extracted bundle when needed.
