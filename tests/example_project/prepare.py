"""tiny-sgd data preparation.

The reference autoresearcher structure keeps fixed data prep in prepare.py.
tiny-sgd generates its synthetic data deterministically inside train.py, so
there is nothing to download or cache here — this file exists to mirror the
expected experiment-repo layout.
"""

if __name__ == "__main__":
    print("tiny-sgd: no preparation needed — data is generated in train.py")
