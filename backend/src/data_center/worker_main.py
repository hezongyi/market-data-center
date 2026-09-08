import time


def main() -> None:
    # MVP worker placeholder: queue-backed execution is introduced after the API contract.
    print("market-data-center worker ready", flush=True)
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()

