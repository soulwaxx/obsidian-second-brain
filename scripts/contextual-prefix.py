#!/usr/bin/env python3
"""Refresh retrieval preparation; current BM25 indexes Markdown text directly."""
import argparse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="refresh all wiki pages (accepted for lifecycle hook compatibility)")
    parser.parse_args()
    print("No contextual prefixes required; BM25 indexes Markdown directly")


if __name__ == "__main__":
    main()
