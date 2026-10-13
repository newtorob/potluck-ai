---
{
  "title": "Potluck is open source. Here's why.",
  "description": "Potluck is now public under the MIT license. Why opening the code matters for the people running it, and why it helps us build better software.",
  "author": "Rob",
  "date": "2026-10-13",
  "category": "Company",
  "image": "/blog/potluck-is-open-source/opengraph-image",
  "diagram": "open-source",
  "draft": false
}
---
Potluck is now open source under the [MIT license](https://github.com/newtorob/potluck-ai/blob/main/LICENSE). The code is on [GitHub](https://github.com/newtorob/potluck-ai), including the desktop app, local engine, CLI, mesh networking and coordinator.

I want Potluck to let you use the computers you already own to run the models you want. A gaming PC, a laptop and a spare desktop should be able to contribute to the same job, with each machine holding part of a larger model. We're still testing and improving that work.

Opening the code makes sense for what we're building, and for the people we're asking to use it.

## You should be able to see what runs on your machines

Potluck runs on your computer. It loads models, connects to other machines and handles requests. If you're going to give software that much access, you should be able to look at how it works.

Which machine receives a request? What gets stored? What happens when you turn sharing off? Those are reasonable questions. We need to answer them clearly in the app and the docs. Now the code is available to inspect too.

Publishing it doesn't prove that every part is correct or secure. It gives people a way to check our work, find problems and challenge an explanation that doesn't match the implementation. That is useful to us as well as to the person using Potluck.

## We need to test more than our own setup

Our computers only cover a small part of what people use. Different GPUs, drivers, operating systems and amounts of available memory all matter. A setup that works here can fail somewhere else.

We recently [wrote about running a 32B model across a PC and a Mac](/blog/32b-model-across-pc-and-mac). Both machines held weights, but the Mac could only take a small share because it had little free memory. Details like that are why I care about testing on ordinary computers people also use for other things.

With the source available, someone who hits a problem can investigate it, share a reproduction or propose a fix. They can also read what we're doing before spending time trying a setup we haven't tested. We still have to review changes and verify what we ship. More people being able to examine the work should help us find things we'd otherwise miss.

## People can take it further

There are uses for Potluck we won't think of, and changes we won't get to first. Someone might want a different model workflow, an integration with a tool they already use, or a way to manage a particular collection of machines.

MIT lets people use, modify and redistribute the code, including commercially, subject to the license's notice requirements. You can build on it or keep your own fork. Model weights and third-party components have their own licenses.

That helps us too. It gives developers a practical reason to try Potluck and make it useful in their own work. Some may contribute changes back. Others may show us a use case we hadn't considered. I would rather learn from that than make everyone wait for us to build every piece ourselves.

## Come look at the code

There is plenty left to do. Making the repository public doesn't make experimental features finished, and it doesn't replace the testing, maintenance and support we're responsible for.

If you want to help, a useful bug report is a good start. Tell us which version you ran, your hardware, what you tried and what happened. Documentation fixes and tested code changes help too. For a larger feature, open an issue first so we can discuss the approach.

The [contributing guide](https://github.com/newtorob/potluck-ai/blob/main/CONTRIBUTING.md) has the setup and testing instructions. Please report security issues through [the security policy](https://github.com/newtorob/potluck-ai/blob/main/SECURITY.md), rather than a public issue.
