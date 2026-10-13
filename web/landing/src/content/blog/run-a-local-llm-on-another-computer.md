---
{
  "title": "How to run a local LLM on another computer",
  "description": "Use a model on a computer you own from your laptop. A Potluck setup guide covering pairing, model loading, serving, and the checks that matter when a request fails.",
  "author": "Rob",
  "date": "2026-10-06",
  "category": "Setup guide",
  "image": "/blog/run-a-local-llm-on-another-computer/opengraph-image",
  "draft": false
}
---
You have a laptop you like working on and another computer that can run the model you want. You want to stay on the laptop and let the other computer handle inference.

That's what **My Machines** in Potluck is for. Your request travels to a paired computer, the model runs there, and the answer comes back to you. The computer doing the work needs enough memory to run the whole model.

This guide covers the desktop app: **0.1.7 on Apple Silicon Mac, 0.1.8 on Linux, and 0.1.5 on Windows**. It covers computers you own. Trusted circles, the open pool, and experimental model splitting are outside this setup.

## What you need

- Potluck installed on both computers, with setup and sign-in completed.
- A model that fits the computer that will answer your requests.
- Both computers awake, running Potluck, and connected to the network.
- Permission to enable Potluck's mesh service when your operating system asks.

Apple Silicon Macs use the Apple GPU. **Linux 0.1.8 supports Vulkan GPU acceleration, with CPU fallback.** It has been validated on an NVIDIA RTX 2080 Ti and Intel integrated graphics; AMD validation is still pending. Check the app's diagnostics to confirm which backend is actually running. A GPU isn't automatically faster for every model and machine.

The Linux downloads are for x86-64 computers. Intel Mac downloads are currently paused. Windows installers are unsigned and may show a SmartScreen warning. The [install page](/install) has the platform requirements, downloads, and published checksums.

## 1. Get a local reply on the serving computer

Start on the computer that will run the model. In **Models**, download a model that fits its available memory and load it. Send a short message in local chat.

Wait for an actual answer before moving on. A downloaded model is not necessarily a loaded model. If it cannot answer locally, pairing another computer won't fix that.

Leave some memory for the operating system and the applications you have open. A model that barely fits may become difficult to use as the conversation grows.

## 2. Pair the computers

Open **My Machines** and follow the pairing flow. Create or join your household as prompted, and use the pairing code to connect the second computer. Confirm that you're pairing the computer you intended.

Enable the mesh when prompted. Potluck uses WireGuard to carry traffic between your machines, and the operating system may ask you to approve a background service or network adapter. You don't need a separate VPN account.

Check the connection state in My Machines before continuing. Seeing a computer listed is not the same as being able to reach it. For the first setup, putting both computers on the same network makes troubleshooting easier.

## 3. Enable serving

On the computer with the model loaded, open **Network** and enable **Contribute compute**. In these releases, that setting allows your paired machines to request inference. It does not make this a dependable public open-pool contributor.

The serving computer needs to remain awake with Potluck running and the model loaded. Closing the app or letting the computer sleep can make it unavailable.

## 4. Send a request from your laptop

Open chat on the laptop and select **My Machines** as the routing scope. Choose a model available on an eligible paired machine, then send a short prompt.

Try something simple first, such as asking for three ideas for a weekend project. Check the response's routing information to confirm which scope served it. A successful local reply alone doesn't prove that the remote route is working.

My Machines selects an eligible machine for the requested model. It does not combine all the RAM or GPUs in your household into one larger device.

## If it doesn't answer

Check the serving computer first. Most of the useful questions are ordinary ones:

| What you see | What to check |
| --- | --- |
| The other computer is unavailable | Is it awake? Is Potluck open? Does My Machines show a reachable connection? |
| The machine is connected but no model is available | Is the model loaded, and is Contribute compute enabled on that machine? |
| The model fails while loading | Try a smaller model or close other memory-heavy applications. |
| A request is much slower than expected | Confirm which computer is serving it. Check CPU versus GPU use and the network connection. Linux 0.1.8 can use Vulkan or fall back to CPU; confirm which backend is active. |
| The mesh won't connect | Check its readiness messages and whether another VPN or a restrictive network is interfering. |

A direct connection is preferred. Potluck can use a relay for encrypted traffic when a direct path is unavailable, but some networks still need troubleshooting. Keep the exact error message if you need help; it tells us more than “it doesn't work.”

## Where the prompt goes

In local mode, the model runs on the computer in front of you. With My Machines, the prompt goes to a paired computer so it can generate the answer. WireGuard encrypts the traffic in transit; the serving computer still processes the prompt itself.

Only pair computers you control and trust. Chat history and saved memories are stored locally by the app. Pairing machines does not automatically synchronize their memory stores.

Start by getting one model to answer on one computer. Then get that same model answering from the other computer. Once those two steps work, you have a useful setup to build on.
