# tiny-sgd - research ideas (generated exploration sequence)

#
- idea_id: `baseline`
- Description: `Unmodified baseline: plain SGD, lr 0.008, 40 steps.`
- EV Improvement: `0.0`
- Why: `Establishes the reference val_mse.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.008, "momentum": 0.0, "steps": 40, "seed": 0}`
#
- idea_id: `more-steps-80`
- Description: `Double the step budget to 80.`
- EV Improvement: `0.2`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.008, "momentum": 0.0, "steps": 80, "seed": 0}`
#
- idea_id: `more-steps-150`
- Description: `Train for 150 steps.`
- EV Improvement: `0.3`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.008, "momentum": 0.0, "steps": 150, "seed": 0}`
#
- idea_id: `more-steps-300`
- Description: `Train for 300 steps.`
- EV Improvement: `0.24`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.008, "momentum": 0.0, "steps": 300, "seed": 0}`
#
- idea_id: `more-steps-600`
- Description: `Brute force: 600 steps at the safe lr.`
- EV Improvement: `0.32`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.008, "momentum": 0.0, "steps": 600, "seed": 0}`
#
- idea_id: `lr-0.02`
- Description: `Raise the learning rate to 0.02.`
- EV Improvement: `0.33`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.02, "momentum": 0.0, "steps": 150, "seed": 0}`
#
- idea_id: `lr-0.05`
- Description: `Raise the learning rate to 0.05.`
- EV Improvement: `0.14`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.05, "momentum": 0.0, "steps": 150, "seed": 0}`
#
- idea_id: `lr-0.1`
- Description: `Raise the learning rate to 0.1.`
- EV Improvement: `0.12`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.1, "momentum": 0.0, "steps": 150, "seed": 0}`
#
- idea_id: `lr-0.15`
- Description: `Raise the learning rate to 0.15.`
- EV Improvement: `0.4`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.15, "momentum": 0.0, "steps": 150, "seed": 0}`
#
- idea_id: `lr-0.2`
- Description: `Raise the learning rate to 0.2.`
- EV Improvement: `0.21`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.2, "momentum": 0.0, "steps": 150, "seed": 0}`
#
- idea_id: `lr-0.3`
- Description: `Push the learning rate to 0.3.`
- EV Improvement: `0.2`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.3, "momentum": 0.0, "steps": 150, "seed": 0}`
#
- idea_id: `lr-0.45`
- Description: `Aggressive lr 0.45.`
- EV Improvement: `0.45`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.45, "momentum": 0.0, "steps": 150, "seed": 0}`
#
- idea_id: `lr-0.6`
- Description: `Aggressive lr 0.6 - near the stability bound.`
- EV Improvement: `0.28`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.6, "momentum": 0.0, "steps": 150, "seed": 0}`
#
- idea_id: `lr-0.8-unstable`
- Description: `lr 0.8 - expected to diverge.`
- EV Improvement: `0.4`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.8, "momentum": 0.0, "steps": 150, "seed": 0}`
#
- idea_id: `lr-0.95-unstable`
- Description: `lr 0.95 - expected to diverge.`
- EV Improvement: `0.28`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.95, "momentum": 0.0, "steps": 150, "seed": 0}`
#
- idea_id: `mom-0.5`
- Description: `Add momentum 0.5 at lr 0.05.`
- EV Improvement: `0.33`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.05, "momentum": 0.5, "steps": 150, "seed": 0}`
#
- idea_id: `mom-0.8`
- Description: `Add momentum 0.8 at lr 0.05.`
- EV Improvement: `0.17`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.05, "momentum": 0.8, "steps": 150, "seed": 0}`
#
- idea_id: `mom-0.9`
- Description: `Heavy-ball momentum 0.9 at lr 0.05.`
- EV Improvement: `0.33`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.05, "momentum": 0.9, "steps": 150, "seed": 0}`
#
- idea_id: `mom-0.95`
- Description: `Momentum 0.95 at lr 0.05.`
- EV Improvement: `0.41`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.05, "momentum": 0.95, "steps": 150, "seed": 0}`
#
- idea_id: `lr0.1-mom0.9`
- Description: `Combine lr 0.1 with momentum 0.9.`
- EV Improvement: `0.29`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.1, "momentum": 0.9, "steps": 150, "seed": 0}`
#
- idea_id: `lr0.15-mom0.9`
- Description: `Combine lr 0.15 with momentum 0.9.`
- EV Improvement: `0.36`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.15, "momentum": 0.9, "steps": 150, "seed": 0}`
#
- idea_id: `lr0.2-mom0.9`
- Description: `Combine lr 0.2 with momentum 0.9.`
- EV Improvement: `0.34`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.2, "momentum": 0.9, "steps": 150, "seed": 0}`
#
- idea_id: `lr0.2-mom0.95`
- Description: `lr 0.2 + momentum 0.95 - may oscillate.`
- EV Improvement: `0.14`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.2, "momentum": 0.95, "steps": 150, "seed": 0}`
#
- idea_id: `lr0.3-mom0.9-unstable`
- Description: `lr 0.3 + momentum 0.9 - likely unstable.`
- EV Improvement: `0.37`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.3, "momentum": 0.9, "steps": 150, "seed": 0}`
#
- idea_id: `lr0.1-mom0.9-300`
- Description: `Best momentum setting, 300 steps.`
- EV Improvement: `0.32`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.1, "momentum": 0.9, "steps": 300, "seed": 0}`
#
- idea_id: `lr0.15-mom0.9-300`
- Description: `lr 0.15 + momentum 0.9, 300 steps.`
- EV Improvement: `0.22`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.15, "momentum": 0.9, "steps": 300, "seed": 0}`
#
- idea_id: `lr0.2-mom0.9-300`
- Description: `lr 0.2 + momentum 0.9, 300 steps.`
- EV Improvement: `0.13`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.2, "momentum": 0.9, "steps": 300, "seed": 0}`
#
- idea_id: `lr0.15-mom0.85-200`
- Description: `lr 0.15 + momentum 0.85, 200 steps.`
- EV Improvement: `0.41`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.15, "momentum": 0.85, "steps": 200, "seed": 0}`
#
- idea_id: `lr0.12-mom0.9-250`
- Description: `Fine-tune around the best region.`
- EV Improvement: `0.28`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.12, "momentum": 0.9, "steps": 250, "seed": 0}`
#
- idea_id: `lr0.18-mom0.88-220`
- Description: `Fine-tune around the best region.`
- EV Improvement: `0.36`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.18, "momentum": 0.88, "steps": 220, "seed": 0}`
#
- idea_id: `lr0.14-mom0.92-280`
- Description: `Fine-tune around the best region.`
- EV Improvement: `0.41`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.14, "momentum": 0.92, "steps": 280, "seed": 0}`
#
- idea_id: `lr0.16-mom0.9-260`
- Description: `Fine-tune around the best region.`
- EV Improvement: `0.36`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.16, "momentum": 0.9, "steps": 260, "seed": 0}`
#
- idea_id: `lr0.13-mom0.9-300`
- Description: `Fine-tune around the best region.`
- EV Improvement: `0.42`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.13, "momentum": 0.9, "steps": 300, "seed": 0}`
#
- idea_id: `lr0.17-mom0.9-300`
- Description: `Fine-tune around the best region.`
- EV Improvement: `0.25`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.17, "momentum": 0.9, "steps": 300, "seed": 0}`
#
- idea_id: `lr0.15-mom0.9-400`
- Description: `Best region, extend to 400 steps.`
- EV Improvement: `0.38`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.15, "momentum": 0.9, "steps": 400, "seed": 0}`
#
- idea_id: `lr0.15-mom0.9-600`
- Description: `Best region, extend to 600 steps.`
- EV Improvement: `0.27`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.15, "momentum": 0.9, "steps": 600, "seed": 0}`
#
- idea_id: `seed-1`
- Description: `Re-run the best config with seed 1.`
- EV Improvement: `0.43`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.15, "momentum": 0.9, "steps": 400, "seed": 1}`
#
- idea_id: `seed-7`
- Description: `Re-run the best config with seed 7.`
- EV Improvement: `0.41`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.15, "momentum": 0.9, "steps": 400, "seed": 7}`
#
- idea_id: `seed-42`
- Description: `Re-run the best config with seed 42.`
- EV Improvement: `0.15`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.15, "momentum": 0.9, "steps": 400, "seed": 42}`
#
- idea_id: `seed-99`
- Description: `Re-run the best config with seed 99.`
- EV Improvement: `0.16`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.15, "momentum": 0.9, "steps": 400, "seed": 99}`
#
- idea_id: `lr0.5-mom0.5-unstable`
- Description: `lr 0.5 + momentum 0.5 - stress test.`
- EV Improvement: `0.19`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.5, "momentum": 0.5, "steps": 150, "seed": 0}`
#
- idea_id: `lr0.05-mom0.99-unstable`
- Description: `Momentum 0.99 - likely to ring.`
- EV Improvement: `0.44`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.05, "momentum": 0.99, "steps": 150, "seed": 0}`
#
- idea_id: `lr0.16-mom0.91-450`
- Description: `Final fine-tune of the incumbent.`
- EV Improvement: `0.26`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.16, "momentum": 0.91, "steps": 450, "seed": 0}`
#
- idea_id: `lr0.15-mom0.9-500`
- Description: `Incumbent at 500 steps.`
- EV Improvement: `0.33`
- Why: `Part of the agent's hyperparameter exploration of the well-conditioned least-squares landscape.`
- Status: `Not Implemented`
- HPPs: `{"lr": 0.15, "momentum": 0.9, "steps": 500, "seed": 0}`
#
