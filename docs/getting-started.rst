Getting Started
===============

Installation
------------

The SDK ships with the `lium.io` package on PyPI:

.. code-block:: bash

   pip install lium.io

Managed binary installs are also available for macOS and Linux on amd64 and arm64:

.. code-block:: bash

   curl -fsSL https://github.com/Datura-ai/lium-cli/releases/latest/download/install.sh | bash

Fresh binary installs create ``~/.lium/bin/lium`` as a managed symlink pointing at a
versioned binary under ``~/.lium/versions/<version>/lium``.

Authentication requires an API key stored in ``~/.lium/config.ini`` or exported as
``LIUM_API_KEY``. The CLI (`lium init`) can bootstrap this for you.

Example
-------

The ``@lium.machine`` decorator is the easiest way to offload work to a GPU pod.

.. code-block:: python

   import lium

   @lium.machine(machine="A100", requirements=["torch", "transformers", "accelerate"])
   def infer(prompt: str) -> str:
       from transformers import AutoTokenizer, AutoModelForCausalLM
       tokenizer = AutoTokenizer.from_pretrained("sshleifer/tiny-gpt2")
       model = AutoModelForCausalLM.from_pretrained("sshleifer/tiny-gpt2", device_map="cuda")
       tokens = tokenizer(prompt, return_tensors="pt").to("cuda")
       out = model.generate(**tokens, max_new_tokens=50)
       return tokenizer.decode(out[0], skip_special_tokens=True)

   print(infer("Who discovered penicillin?"))

Direct SDK usage follows the same pattern:

.. code-block:: python

   from lium.sdk import Lium

   lium = Lium()
   node = lium.ls(gpu_type="A100")[0]
   pod = lium.up(executor_id=node.id, name="demo")
   ready = lium.wait_ready(pod, timeout=600)
   print(lium.exec(ready, command="nvidia-smi")["stdout"])

Most pod-level SDK calls (`exec`, `down`, `backup_*`, etc.) expect a :class:`lium.sdk.PodInfo`
instance. Use `lium.ps()` or `lium.wait_ready()` to obtain the dataclass before passing the pod to
other methods.

First hour on a Lium pod
------------------------

A short checklist for getting a freshly rented pod into a productive state. Everything below
runs inside the pod (``lium ssh <pod>`` or ``lium exec <pod> "..."``).

**Always set a lifetime.** A pod bills until it is removed. Pass ``--ttl`` (or ``--until``) on
every ``lium up`` so a forgotten pod terminates on its own:

.. code-block:: bash

   lium up --gpu H200 -c 8 --ttl 6h --yes

**Put weights and data under /workspace.** With volume encryption enabled (the default), the
pod's local volume — normally ``/root`` — is an encrypted FUSE mount. It is the right place for
small, persistent files, but large sequential reads such as loading model weights are noticeably
slower there. Keep checkpoints, datasets and the Hugging Face cache on ``/workspace`` (create it
if the image does not have it) and point the cache there before the first download:

.. code-block:: bash

   mkdir -p /workspace/hf
   export HF_HOME=/workspace/hf
   export HF_HUB_ENABLE_HF_TRANSFER=1   # faster downloads; needs `pip install hf_transfer`

**Installing Python packages on Ubuntu 24.04 images.** System Python is externally managed
(PEP 668), so a bare ``pip install`` fails. Either create a virtual environment or opt out
explicitly:

.. code-block:: bash

   python -m venv /workspace/venv && source /workspace/venv/bin/activate
   # or
   export PIP_BREAK_SYSTEM_PACKAGES=1

**Blackwell GPUs need a recent CUDA build of PyTorch.** B200, B300, RTX PRO 6000 and RTX 5090
(compute capability 10.x / 12.x) are not supported by wheels built for CUDA 12.4 or older. Install
a cu128 or newer build, for example:

.. code-block:: bash

   pip install torch --index-url https://download.pytorch.org/whl/cu130

FlashAttention-3 targets Hopper (H100/H200) only. On Blackwell use FlashAttention-4 or the cuDNN
attention backend (``torch.nn.attention.sdpa_kernel``) instead.

**Common system packages.** Minimal images may lack tools that training and data scripts assume:

.. code-block:: bash

   apt-get update && apt-get install -y ffmpeg rsync

**Background jobs.** A plain ``nohup cmd &`` started through ``lium exec`` keeps the SSH session
open and can die with it. Detach the process from the session and close its stdin:

.. code-block:: bash

   nohup setsid python train.py > /workspace/logs/train.log 2>&1 < /dev/null &

``lium exec <pod> -d "python train.py"`` does the same thing for you and prints the PID and log
path.

**Check that the GPUs are being used.** Sample utilisation to a CSV while a job runs, then look at
it with any tool:

.. code-block:: bash

   nvidia-smi --query-gpu=timestamp,index,utilization.gpu,memory.used \
     --format=csv -l 5 > /workspace/logs/gpu.csv &

Use ``lium up --verify-gpus`` to have the CLI compare the GPU count ``nvidia-smi`` reports with
the count the pod is billed for.

vLLM Deployment
~~~~~~~~~~~~~~~

A more complete example showing how to deploy vLLM on the Lium platform:

.. literalinclude:: ../examples/quick_vllm.py
   :language: python
   :linenos:
