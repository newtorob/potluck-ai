---
{
  "title": "We ran a 32B model across a PC and a Mac",
  "description": "A Potluck development test split Qwen2.5-Coder 32B across an RTX 2080 Ti PC and an M2 Pro Mac. Here's where the model ran and what still needs work.",
  "author": "Rob",
  "date": "2026-10-09",
  "category": "Build notes",
  "image": "/blog/32b-model-across-pc-and-mac/opengraph-image",
  "diagram": "split-inference",
  "draft": false
}
---
If you have a gaming PC, a laptop and an older desktop, I want Potluck to let you use them together to run a larger model. Each computer holds part of it and does part of the work. The memory in one machine shouldn't be the limit for the whole setup.

On September 29, we ran Qwen2.5-Coder 32B across a Linux PC and an M2 Pro Mac using a development build of Potluck. Both computers held model weights and took part in generating the responses.

Part of the model still ran on the PC's CPU. The Mac could only take a small share because it had little free memory at the time.

## The computers we used

| Computer | Hardware | Connection |
| --- | --- | --- |
| Linux PC | RTX 2080 Ti with 11 GiB of VRAM, 64 GiB of system RAM | Ethernet |
| Mac | M2 Pro with 16 GiB of unified memory | Wi-Fi |

We used the Q3_K_M quantization of Qwen2.5-Coder-32B-Instruct, a roughly 16 GB model file, with an 8,192-token context. Quantization reduces the memory needed to store the model's weights. The exact version matters when comparing whether a model fits.

We left unrelated applications open. These were development runtimes, separate from the installed apps.

## Where the model ended up

Potluck checked the actual placement before reporting the split ready.

| Location | Model weights |
| --- | ---: |
| PC system memory, running on CPU | 6.23 GiB |
| PC GPU | 7.58 GiB |
| Mac worker | 1.03 GiB |

These numbers describe the model weights. The applications need additional memory to run.

The Mac had 16 GiB installed, but only 3.50 GiB was available when Potluck planned the split. Potluck kept 2 GiB in reserve, leaving a 1.50 GiB worker budget. About 1.03 GiB of model weights ended up there.

That's why adding the numbers on the spec sheets won't tell you what a setup can run. A computer you use for other things has less memory to contribute. Potluck needs to account for that, and show you how much each machine is actually doing.

In separate tests with tighter memory budgets, the Mac received no model weights at all. Potluck refused those attempts. If you select a computer to take part in a split, it should have to take part before the app says the split is ready.

## How the run went

The final split took 43.8 seconds to become ready. A short prompt asking for an integer sequence completed in 25.5 seconds. A longer prompt containing 80 synthetic ledger lines completed in 31.3 seconds. Each response contained 128 generated tokens. We also checked that both requests used the intended model file.

Those are two small test requests. They establish that the model loaded across both computers and generated responses. They don't establish how well it handles a coding project or a long conversation.

The PC also had enough system RAM to hold this model by itself. We haven't shown that splitting was necessary for this model to fit, or that this setup was faster than running it on the PC alone.

## What I want this to become

I want someone to be able to look at the computers they already own and work out whether those machines can run the model they want. Potluck needs to handle the split, report where the model is running, and make it clear when the setup isn't going to work.

For this configuration, the next useful tests need more available Mac memory and a wired connection at both ends. A comparison against the PC alone needs the same model, quantization, context and prompts. Then we need to run useful tasks through it and measure whether the result is worth the wait.

This test used development builds. The [model-splitting page](/how-it-works#model-splitting) explains how Potluck divides the work and which parts are available to try.
