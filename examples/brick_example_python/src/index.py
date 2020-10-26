import os

CURRENT_FOLDER = os.path.dirname(os.path.abspath(__file__))
NODE_OUT_TXT = os.path.join(CURRENT_FOLDER, "../../brick_example_node/dist/out.txt")

if __name__ == "__main__":
    with open(NODE_OUT_TXT) as f:
        print(f"{f.read().strip()} and Python")

