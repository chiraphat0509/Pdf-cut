import sys
from splitter.server import serve

if __name__ == "__main__":
    serve(open_browser="--no-browser" not in sys.argv)
