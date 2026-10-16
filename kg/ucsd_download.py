from .files import graph_data_dir
from .http_client import download_file

UCSD_FILE_URLS = {
    "steam_games.json.gz": "https://cseweb.ucsd.edu/~wckang/steam_games.json.gz",
    "australian_users_items.json.gz": "https://mcauleylab.ucsd.edu/public_datasets/data/steam/australian_users_items.json.gz",
    "australian_user_reviews.json.gz": "https://mcauleylab.ucsd.edu/public_datasets/data/steam/australian_user_reviews.json.gz",
}


def download_ucsd_files() -> None:
    directory = graph_data_dir("ucsd")
    for filename, url in UCSD_FILE_URLS.items():
        destination = directory / filename
        if destination.exists():
            continue
        print(f"ucsd: downloading {filename}")
        download_file(url, destination)
