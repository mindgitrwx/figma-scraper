import glob
import logging
from multiprocessing import cpu_count
import random
import threading
import click
import time
import json
import os
import re
import shutil
import tempfile
import requests
from requests.adapters import HTTPAdapter
from urllib.parse import urlencode
from urllib3 import Retry
import backoff
from ssl import SSLError
from pathlib import Path
from dotenv import load_dotenv
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
import queue
from typing import List, Tuple
import resource
from PIL import Image
import math
import io



resource.setrlimit(
    resource.RLIMIT_CORE,
    (resource.RLIM_INFINITY, resource.RLIM_INFINITY))


load_dotenv()

API_BASE_URL = "https://api.figma.com/v1"
BOTTOM_POSITION = 24

@click.command()
@click.option("-v", "--version", default=0, type=click.INT, help="Version number to specify cache - update for new versions")
@click.option("-dir", default="./downloads", type=click.Path(exists=True, file_okay=False, dir_okay=True), help="Directory containing the JSON files")
@click.option("-fmt", '--format',  default="png", help="Image format to bake the layers")
@click.option("-s", '--scale', default="1", help="Image scale")
@click.option("-d", '--depth',  default=None, help="Layer depth to go recursively", type=click.INT)
@click.option('--skip-canvas',  default=True, help="Skips the canvas while exporting images")
@click.option('--no-fills',  default=False, help="Skips the download for Image fills")
@click.option("--optimize", is_flag=True, help="Optimize images size (Now only applied to hash images)", default=False, type=click.BOOL)
@click.option("--max-mb-hash", help="Max mb to be applied to has images (if optimize is true)", default=1, type=click.INT)
@click.option("-t", "--figma-token", help="Figma API access token.", default=os.getenv("FIGMA_ACCESS_TOKEN"), type=str)
@click.option("-src", '--source-dir', default="./downloads/*.json", help="Path to the JSON file")
@click.option("-c", "--concurrency", help="Number of concurrent processes.", default=cpu_count(), type=int)
@click.option("--skip", help="Number of files to skip (for dubugging).", default=0, type=int)
@click.option("--no-download", is_flag=True, help="No downloading the images (This can be used if you want this script to only run for optimizing existing images)", default=0, type=int)
@click.option("--shuffle", is_flag=True, help="Rather if to randomize the input for even distribution", default=False, type=click.BOOL)
def main(version, dir, format, scale, depth, skip_canvas, no_fills, optimize, max_mb_hash, figma_token, source_dir, concurrency, skip, no_download, shuffle):
    # progress bar position config
    global BOTTOM_POSITION
    BOTTOM_POSITION = concurrency * 2 + 5

    # figma token
    if figma_token.startswith("[") and figma_token.endswith("]"):
      figma_tokens = json.loads(figma_token)
    else:
      figma_tokens = [figma_token]

    if not optimize:
      max_mb_hash = 0

    img_queue = queue.Queue()
    root_dir = Path(dir)

    _src_dir = Path('/'.join(source_dir.split("/")[0:-1]))   # e.g. ./downloads
    _src_file_pattern = source_dir.split("/")[-1]            # e.g. *.json
    json_files = glob.glob(_src_file_pattern, root_dir=_src_dir)
    json_files = json_files[skip:]
    file_keys = [Path(file).stem for file in json_files]

    # randomize for even distribution
    if shuffle:
      shuffled = [item for item in range(len(json_files))]
      random.shuffle(shuffled)
      json_files = [json_files[i] for i in shuffled]
      file_keys = [file_keys[i] for i in shuffled]

    # download thread
    download_thread = threading.Thread(target=image_queue_handler, args=(img_queue,))
    download_thread.start()

    # main progress bar
    pbar = tqdm(total=len(json_files), position=BOTTOM_POSITION, leave=True)

    chunks = chunked_zips(file_keys, json_files, n=concurrency)
    threads: list[threading.Thread] = []

    tqdm.write(f"🔥 {concurrency} threads / {len(figma_tokens)} identities")

    # run the main thread loop
    size_avg = len(json_files) // concurrency # a accurate enough estimate for the progress bar. this is required since we cannot consume the zip iterator. - which means cannot get the size of the files inside each thread. this can be improved, but we're keeping it this way.
    for _ in range(concurrency):
      t = threading.Thread(target=process_files, args=(chunks[_],), kwargs={
        'root_dir': root_dir,
        'src_dir': _src_dir,
        'img_queue': img_queue,
        'skip_canvas': skip_canvas,
        'no_fills': no_fills,
        'figma_token': figma_tokens[(_ + 1) % len(figma_tokens)],
        'format': format,
        'scale': scale,
        'optimize': optimize,
        'max_mb_hash': max_mb_hash,
        'depth': depth,
        'size': size_avg,
        'index': _,
        'pbar': pbar,
        'no_download': no_download,
        'concurrency': concurrency
      })
      t.start()
      threads.append(t)
      # # give each thread a little time difference to prevent 429
      # time.sleep(10)

    for t in threads:
      t.join()

    tqdm.write("All done!")
    # Signal the handler to stop by adding a None item
    img_queue.put(('EOD', 'EOD', None))
    # finally wait for the download thread to finish
    download_thread.join()

def process_files(files, root_dir: Path, src_dir: Path, img_queue: queue.Queue, skip_canvas: bool, no_fills: bool, figma_token: str, format: str, scale: int, optimize: bool, max_mb_hash: int, depth: int, index: int, size: int, pbar: tqdm, concurrency: int, no_download: bool):
    # for key, json_file in files:
    for key, json_file in tqdm(files, desc=f"⚡️ {figma_token[:8]}", position=BOTTOM_POSITION-(index+4), leave=True, total=size):
        subdir: Path = root_dir / key
        subdir.mkdir(parents=True, exist_ok=True)

        json_file = src_dir / Path(json_file)
        file_data = read_file_data(json_file)

        if file_data:
          if depth is not None:
              depth = int(depth)
          # fetch and save thumbnail (if not already downloaded)
          if not (subdir / "thumbnail.png").is_file() and not no_download:
            thumbnail_url = file_data["thumbnailUrl"]
            download_image(thumbnail_url, subdir / "thumbnail.png")
            # tqdm.write(f"Saved thumbnail to {subdir / 'thumbnail.png'}")

          node_ids = get_node_ids(
              file_data, depth=depth, skip_canvas=skip_canvas)
          # ----------------------------------------------------------------------
          # image fills
          if not no_fills:
            images_dir = subdir / "images"
            images_dir.mkdir(parents=True, exist_ok=True)
            existing_images = os.listdir(images_dir)

            # TODO: this is not safe. the image fills still can be not complete if we terminate during the download
            # Fetch and save image fills (B)
            if len(existing_images) == 0 and not no_download:
                # tqdm.write("Fetching image fills...")
                image_fills = fetch_file_images(key, token=figma_token)
                url_and_path_pairs = [
                    (url, os.path.join(images_dir, f"{hash_}.{format}"))
                    for hash_, url in image_fills.items()
                ]

                # we don't use queue for has images
                fetch_and_save_images(url_and_path_pairs, max_mb=max_mb_hash)
                # for pair in url_and_path_pairs:
                #   img_queue.put(pair + (max_mb_hash,))
            else:
                # tqdm.write(f"{images_dir} - Image fills already fetched")
                ...
                if optimize:
                    for image in existing_images:
                        file = images_dir / image
                        success, saved = optimize_image(file, max_mb=max_mb_hash)                
                        if success:
                            tqdm.write(f"☑ {fixstr(f'(existing) Saved {(saved / 1024 / 1024):.2f}MB')}... → {file}")

          # ----------------------------------------------------------------------
          # bakes
          images_dir = subdir / "bakes"
          images_dir.mkdir(parents=True, exist_ok=True)
          existing_images = os.listdir(images_dir)

          # Fetch and save layer images (A)
          node_ids_to_fetch = [
              node_id
              for node_id in node_ids
              if f"{node_id}.{format}" not in existing_images
              and f"{node_id}@{scale}x.{format}" not in existing_images
          ]

          if node_ids_to_fetch and not no_download:
              # tqdm.write(f"Fetching {len(node_ids_to_fetch)} of {len(node_ids)} layer images...")
              layer_images = fetch_node_images(
                  key, node_ids_to_fetch, scale, format, token=figma_token, position=BOTTOM_POSITION-((concurrency*2)+index), conncurrency=concurrency)
              url_and_path_pairs = [
                  (
                      url,
                      os.path.join(
                          images_dir,
                          f"{node_id}{'@' + str(scale) + 'x' if scale != '1' else ''}.{format}",
                      ),
                  )
                  for node_id, url in layer_images.items()
              ]
              for pair in url_and_path_pairs:
                  img_queue.put(pair + (None,))
          else:
              # tqdm.write(f"{images_dir} - Layer images already fetched")
              ...

          tqdm.write(f"☑ {subdir}")
        else:
          tqdm.write(f"☒ {subdir}")
        pbar.update(1)


def requests_retry_session(
    retries=3,
    backoff_factor=1,
    status_forcelist=(500, 502, 504),
    session=None,
):
    session = session or requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session



@backoff.on_exception(
    backoff.expo, (requests.exceptions.RequestException, SSLError), max_tries=5,
    logger=logging.getLogger('backoff').addHandler(logging.StreamHandler())
)
def download_image(url, output_path, max_mb=None, timeout=10):
    if url is None:
        return None, None
    try:
        response = requests_retry_session().get(url, stream=True, timeout=timeout)
        response.raise_for_status()
        with open(output_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
            f.close()

        if max_mb is not None and max_mb > 0:
            success, saved = optimize_image(output_path, max_mb=max_mb)
            if (success):
                tqdm.write(f"☑ {fixstr(f'Optimized - saved {(saved / 1024 / 1024):.2f}MB')}... → {output_path}")
        return url, output_path
    # check if 403 Forbidden
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 403:
            log_error(f"☒ {fixstr(f'Forbidden (Expired): {url}')}", print=True)
            return None, None
        else:
            tqdm.write(f"☒ Error downloading {url}: {e}")
            return None, None
    except Exception as e:
        tqdm.write(f"☒ Error downloading {url}: {e}")
        return None, None


def download_image_with_progress_bar(item, progress):
    url, path, max_mb = item
    download_image(url, path, max_mb=max_mb)
    progress.update(1)

def image_queue_handler(img_queue: queue.Queue, batch=64, timeout=3600):
    emojis = ['📭', '📬', '📫']
    
    total = 0
    while True:
        items_to_process = []
        batch_start_time = time.time()
        timeout_time = batch_start_time + timeout
        url = None

        progress = tqdm(total=batch, desc=f"📭 ({total}/{total+img_queue.qsize()})", position=BOTTOM_POSITION-2, leave=False)

        while len(items_to_process) < batch:
            try:
                url, path, max_mb = img_queue.get(timeout=1)
                if url == 'EOD':  # Check for sentinel value ('EOD', 'EOD')
                    break
                
                if url is not None:
                  items_to_process.append((url, path, max_mb))
                  total += 1
                  progress.desc = f"📭 ({total}/{len(items_to_process)}/{batch}/{total}/{total+img_queue.qsize()})"
                  batch_start_time = time.time()  # Update the batch start time
            except queue.Empty:
                if time.time() > timeout_time:
                    tqdm.write("⏰ Image Archiving Timed Out")
                    break

        if time.time() > timeout_time:
            break

        if url == 'EOD':  # Break the outer loop if sentinel value is encountered
            tqdm.write("⏰ sentinel value encountered")
            break

        if not items_to_process:
            break
        
        progress.desc = f"{random.choice(emojis)}"
        with ThreadPoolExecutor(max_workers=batch) as executor:
            download_func = partial(download_image_with_progress_bar, progress=progress)
            executor.map(download_func, items_to_process)

        time.sleep(0.1)
        progress.close()

    tqdm.write("✅ Image Archiving Complete")

def fetch_and_save_images(url_and_path_pairs, max_mb=1, position=5, num_threads=64):
    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = {executor.submit(download_image, url, path, max_mb): (url, path) for url, path in url_and_path_pairs}

        if position is not None:
            futures = tqdm(as_completed(futures), total=len(futures), desc=f"Downloading images (Utilizing {num_threads} threads)", position=position, leave=False)
        else:
            futures = as_completed(futures)

        for future in futures:
            url, downloaded_path = future.result()
            if downloaded_path:
                tqdm.write(f"☑ {fixstr(url)} → {downloaded_path}")
                if max_mb is not None and max_mb > 0:
                    optimize_image(downloaded_path, max_mb=max_mb)
            else:
                tqdm.write(f"Failed to download image: {url}")

def fixstr(str, n=64):
    """
    if str is longer than n, return first n characters
    if str is shorter than n, return str with spaces added to end
    """
    if len(str) > n:
        return str[:n]
    else:
        return str + " " * (n - len(str))


mb = 1024 * 1024
def optimize_image(path, max_mb=1):
    margin = 0.3
    try:
        startsize = os.path.getsize(path)
        max_size = max_mb * mb
        # Check if the image is already smaller than the target size
        if startsize <= max_size:
            return None, None
        target_bytes = max_size - (margin * mb)
        # Open the image
        img = Image.open(path)
        # Calculate the current number of bytes
        current_bytes = io.BytesIO()
        img.save(current_bytes, format='PNG')
        current_bytes = current_bytes.tell()
        if current_bytes > max_size:
            # Calculate the scale factor
            scale_factor = math.sqrt(target_bytes / current_bytes)
            # Calculate the new size
            new_size = tuple(int(dim * scale_factor) for dim in img.size)
            # Resize the image
            img = img.resize(new_size, resample=Image.BICUBIC)
        # Save the image to a temporary file
        with tempfile.NamedTemporaryFile(mode='wb', suffix='.png', delete=False) as tmp:
            img.save(tmp.name, format='PNG')
            # Remove the original file before copying the temporary file

            # double check if it actually got smaller
            if os.path.getsize(tmp.name) > startsize:
                return False, 0

            # Remove the original file before moving the temporary file to the original filename
            os.remove(path)
            shutil.move(tmp.name, path)
        endsize = os.path.getsize(path)
        saved = startsize - endsize
        return True, saved
    except Exception as e:
        tqdm.write(f"☒ Error optimizing {path}: {e}")
        return False, 0


def fetch_file_images(file_key, token):
    url = f"{API_BASE_URL}/files/{file_key}/images"
    headers = {"X-FIGMA-TOKEN": token}
    try:
      response = requests.get(url, headers=headers)
      data = response.json()
    except requests.exceptions.ConnectionError as e:
        return {}

    if "error" in data and data["error"]:
        raise ValueError("Error fetching image fills")
    try:
      return data["meta"]["images"]
    except KeyError:
      return {}


def fetch_node_images(file_key, ids, scale, format, token, position, conncurrency):
    url = f"{API_BASE_URL}/images/{file_key}"
    headers = {"X-FIGMA-TOKEN": token}
    params = {
        "ids": "",
        "use_absolute_bounds": "true",
        "scale": scale,
        "format": format,
    }

    # figma server allows up to 5000 characters in the url (between 4000 ~ 6000 characters)
    def chunk(ids, url=url, params=params, max_len=5000):
        """
        chunk the ids for the api call to avoid the url max length limit.
        most browsers have a limit of 2048 characters, but for this case, it is safe to increase it to 4000
        use the url and params info to calculate the length of the api call
        the chunked ids will be formatted as id1,id2,id3
        """

        def get_chunk_len(chunk):
            return len(url) + len(",".join(chunk)) + len(urlencode(params))

        chunk = []
        for id_ in ids:
            if get_chunk_len(chunk + [id_]) > max_len:
                yield chunk
                chunk = []
            chunk.append(id_)
        yield chunk


    max_retry = 5
    delay_between_429 = 5
    ids_chunks = list(chunk(ids))


    def fetch_images_chunk(chunk, retry=0):
        params["ids"] = ",".join(chunk)
        try:
            response = requests.get(url, headers=headers, params=params)
        except requests.exceptions.ConnectionError as e:
            return {}
        except requests.exceptions.JSONDecodeError as e:
            return {}
        except json.decoder.JSONDecodeError as e:
            return {}

        if response.status_code == 429:
            if retry >= max_retry:
                log_error(f"Error fetching [{(','.join(chunk))}] layer images. Rate limit exceeded.")
                tqdm.write(f"☒ HTTP429 - Rate limit exceeded. ({max_retry} tries)")
                return {}

            # check if retry-after header is present
            retry_after = response.headers.get("retry-after")
            retry_after = int(
                retry_after) if retry_after else delay_between_429 * (retry + 1) * conncurrency

            if max_retry - retry < 2:
              # only show the last retry message
              tqdm.write(
                  f"☒ HTTP429 - Waiting {retry_after} seconds before retrying...  ({retry + 1}/{max_retry})")
            time.sleep(retry_after)
            return fetch_images_chunk(chunk, retry=retry + 1)

        try:
          data = response.json()
        except requests.exceptions.JSONDecodeError as e:
          return {}
        
        if "err" in data and data["err"]:
            # ignore and report error
            msg = f"Error fetching {len(chunk)} layer images [{','.join(chunk)}], e:{data['err']}"
            log_error(msg)
            return {}
        return data["images"]

    max_concurrent_requests = conncurrency
    delay_between_batches = 1
    num_batches = -(-len(ids_chunks) // max_concurrent_requests)
    batch_chunks = chunked_list(ids_chunks, num_batches)

    # TODO: group the batches to one ThreadPoolExecutor
    # TODO: the below logic seems duplicated.
    emojis = ['🛫', '🛬']
    image_urls = {}
    with tqdm(range(num_batches), desc=f"{random.choice(emojis)} ({len(ids)}/{len(ids_chunks)}/{num_batches})", position=position, leave=False, mininterval=1) as pbar:
        for batch_idx in pbar:
            for _chunk in batch_chunks[batch_idx]:
              image_urls.update(fetch_images_chunk(chunk=_chunk))

            if batch_idx < num_batches - 1:
                wait = delay_between_batches * batch_idx
                pbar.set_description(
                    f"{random.choice(emojis)} {batch_idx + 1 + 1}/{num_batches}: Waiting {wait} seconds before next batch...")
                time.sleep(wait)

    return image_urls


def calculate_program():
    """
    calculates the stats of the program before starting the loop. this is helpful to show the progress bar.
    """
    ...


# utils

def log_error(msg, print=False):
    try:
      if print:
          tqdm.write(msg)

      # check if err log file exists
      err_log_file = Path("err.log")
      if not err_log_file.exists():
          with open(err_log_file, "w") as f:
              f.write("")
              f.close()
      
      with open(err_log_file, "a") as f:
          f.write(msg + "\n")
          f.close()
    except Exception as e:...


def read_file_data(file: Path):
    if file.is_file():
        try:
          with open(file, "r") as file:
            file_data = json.load(file)
            return file_data
        except json.decoder.JSONDecodeError as e:
          log_error(f"Error loading {file} Skipping... (Malformed JSON file)) - error: {e.msg} {e.args}")

          # read the json file and print the start and end of it for debugging
          try:
            with open(file, "r") as file:
                  txt = file.read()
                  _first_few = txt[0: 100]
                  _last_few = txt[-100:]
                  tqdm.write(f"First few characters: \n{_first_few}")
                  tqdm.write(f"Last few characters: \n{_last_few}")
          except TypeError as e: ...
          return None
    else:
        tqdm.write(f"File {file} not found")
        return None



def get_node_ids(data, depth=None, skip_canvas=True):
    """

    In most cases, you want to set depth to 1 or None with skip_canvas=True.

    depth:
     - None means no limit
     - 0 means no nodes, retrurns only canvas ids (if skip_canvas is False) otherwise empty list
     - 1 means only canvas ids (if skip_canvas is False) and direct children of canvas

    ...
    """
    def extract_ids_recursively(node, current_depth):
        if depth is not None and current_depth >= depth:
            return []

        ids = [node["id"]]
        if "children" in node:
            for child in node["children"]:
                ids.extend(extract_ids_recursively(child, current_depth + 1))
        return ids

    if skip_canvas:
        return [
            id_
            for canvas in data["document"]["children"]
            for child in canvas['children']
            for id_ in extract_ids_recursively(child, 0)
        ]

    return [
        id_
        for child in data["document"]["children"]
        for id_ in extract_ids_recursively(child, 0)
    ]


def get_existing_images(images_dir):
    return set(os.listdir(images_dir))





def chunked_zips(a: list, b: list, n: int) -> List[zip]:
    zipsize = len(a)
    per_zip = zipsize // n
    remainder = zipsize - per_zip * n
    zips = []
    for i in range(n - 1):
        start = i * per_zip
        end = start + per_zip
        _a = a[start:end]
        _b = b[start:end]
        zips.append(zip(_a, _b))
    start = (n - 1) * per_zip
    end = start + per_zip + remainder
    _a = a[start:end]
    _b = b[start:end]
    zips.append(zip(_a, _b))
    return zips

def chunked_list(a: list, n: int) -> List[zip]:
    listsize = len(a)
    perlist = listsize // n
    remainder = listsize - perlist * n
    chunks = []
    for i in range(n - 1):
        start = i * perlist
        end = start + perlist
        _a = a[start:end]
        chunks.append(_a)
    start = (n - 1) * perlist
    end = start + perlist + remainder
    _a = a[start:end]
    chunks.append(_a)
    return chunks



# main
if __name__ == "__main__":
    main()
