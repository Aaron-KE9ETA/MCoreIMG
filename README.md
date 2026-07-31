# MCoreIMG
MCoreIMG is a compact image instruction protocol for transmitting simple vector graphics over MeshCore.

Human draws picture → editor creates structured objects → optimizer finds patterns → compressor produces radio messages

It encodes drawing commands into up to five 150-character messages, using 15-character commands for efficient, low-bandwidth image reconstruction.

a human designs it visually and semantically; the software handles deltas, Rice coding, state reuse, and transport encoding. The compressed protocol is a machine target, like assembly language—not the drawing interface
## Pre-Alpha
Pre-Alpha is just to get a local proof of concept running on a desktop environment 
