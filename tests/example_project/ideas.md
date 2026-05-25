# tiny-sgd — research ideas

#
- idea_id: `baseline`
- Description: `Unmodified baseline — plain full-batch gradient descent, lr 0.02, 60 steps.`
- EV Improvement: `0.0`
- Why: `Establishes the reference val_mse that every later run is compared against.`
- Status: `⚪ Not Implemented`
- HPPs: `{"lr": 0.02, "momentum": 0.0, "steps": 60, "seed": 0}`
#
- idea_id: `faster-lr`
- Description: `Raise the learning rate to 0.2 so 60 steps is enough to fully converge.`
- EV Improvement: `0.35`
- Why: `The baseline is badly under-converged at lr 0.02; this is a well-conditioned quadratic that tolerates a far larger step.`
- Status: `⚪ Not Implemented`
- HPPs: `{"lr": 0.2, "momentum": 0.0, "steps": 60, "seed": 0}`
#
- idea_id: `add-momentum`
- Description: `Keep a modest lr 0.05 but add heavy-ball momentum 0.9.`
- EV Improvement: `0.3`
- Why: `Momentum accelerates convergence on a quadratic without the instability of a very large raw learning rate.`
- Status: `⚪ Not Implemented`
- HPPs: `{"lr": 0.05, "momentum": 0.9, "steps": 60, "seed": 0}`
#
- idea_id: `more-steps`
- Description: `Keep the safe baseline lr but train for 400 steps instead of 60.`
- EV Improvement: `0.2`
- Why: `Brute force — more steps at the safe lr will eventually converge, at the cost of wall-clock.`
- Status: `⚪ Not Implemented`
- HPPs: `{"lr": 0.02, "momentum": 0.0, "steps": 400, "seed": 0}`
#
- idea_id: `aggressive-lr`
- Description: `Push the learning rate to 0.8 for the fastest possible convergence.`
- EV Improvement: `0.15`
- Why: `If stable it converges in a handful of steps — but 0.8 is past the stability bound for this curvature and will likely diverge. A deliberate failure case.`
- Status: `⚪ Not Implemented`
- HPPs: `{"lr": 0.8, "momentum": 0.0, "steps": 60, "seed": 0}`
#
