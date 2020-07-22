#! /usr/bin/env python3
"""My Podcaster."""
import datetime
import email.utils
from subprocess import call
import mimetypes
import os
import re
import shutil
import socket
import urllib.error
import urllib.request
import requests
import tqdm
import random
import signal
from Podcast import Podcast
import configparser

mimetypes.init()
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; WOW64; Trident/7.0) like Gecko",
}


def TimedInput(prompt="", default=None, timeout=5):
    """Input with timeout."""
    import threading
    import time

    def print_countdown():
        t = threading.currentThread()
        while getattr(t, "do_run", True):
            time.sleep(1)
            try:
                countdown = int(signal.getitimer(signal.ITIMER_REAL)[0])
                print(countdown + 1, end="..", flush=True)
                if bool(countdown):
                    continue
                return
            except AlarmException:
                return

    class AlarmException(Exception):
        pass

    def alarmHandler(signum, frame):
        raise AlarmException

    signal.signal(signal.SIGALRM, alarmHandler)
    signal.alarm(timeout)
    t = threading.Thread(target=print_countdown)
    t.start()
    try:
        text = input(prompt)
        signal.alarm(0)
        t.do_run = False
        t.join()
        return text
    except AlarmException:
        pass
    signal.signal(signal.SIGALRM, signal.SIG_IGN)
    return default


def getpodcast() -> None:
    """Get Podcast."""
    # print list of podcasts
    get = True
    while get:
        podlist = configparser.ConfigParser()
        podlist.read("/home/pranav/podcasts.ini")
        podchoice = random.choice(list(podlist))
        if podchoice == "DEFAULT":
            continue
        get = process_podcast(podlist[podchoice])


def process_podcast(podchoice):
    """Process Podcast."""
    # if --podcast is used we will only process a matching name
    pod = podchoice["title"]
    url = podchoice["url"]
    print(pod, url)
    if url[:4] == "file":
        newfilename = url[6:]
        print(pod, ":", newfilename)
        ans = TimedInput(
            prompt="Play local copy ? (Y/n) Defaulting in:", default="Y", timeout=5,
        )
        if not ans == "n":
            call(
                [
                    "mpv",
                    "--no-video",
                    "--term-osd-bar",
                    "--term-osd-bar-chars=[##-]",
                    "--msg-level=all=error,statusline=status",
                    newfilename,
                ],
            )
        return True

    if url[:4] == "http":
        try:
            request = urllib.request.Request(url, headers=headers)
            content = urllib.request.urlopen(request)
            podcast = Podcast(content.read())
        except (urllib.error.HTTPError, urllib.error.URLError) as err:
            print(f"Podcast: {pod}")
            print(f"Connection error: {err}")
            return  # continue

        while True:
            item = random.choice(podcast.items)
            if not item.enclosure_type:
                print(item.title, ":", item.link)
                print("Not Playing, No links available")
                return True
            try:
                finish_playing = process_podcast_item(pod, item)
                if finish_playing:
                    return True
                return False
            except SkipPodcast:
                return True
    print("Weird URL in File", url)
    exit()


class SkipPodcast(Exception):
    """Skipping if the podcast isn't found."""

    pass


def process_podcast_item(pod: str, item: dict):
    """Process a single item from pod."""
    # skip if date is older then --date-from
    data = {
        "podcast": pod,
        "date": item.date_time.strftime("%d.%m.%Y"),
        "title": getSafeFilenameFromText(item.title.strip(" .")),  # scrub title
        "year": str(item.date_time.year),
        "ext": parseFileExtensionFromUrl(item.enclosure_url)
        or mimetypes.guess_extension(item.enclosure_type),
    }

    newfilelength = 0
    newfilemtime = item.time_published
    newfilename = (
        "/home/pranav/mytools/random_podcaster/"
        + f"{pod}/{data['title']}_{data['date']}{data['ext']}"
    )
    print(f"Podcast Series:       {pod}")
    print(f"Episode Title:        {data['title']}")
    print(f"Date:                 {data['date']}")
    print(f"Episode Description:  {item.description}")

    ans = TimedInput(
        prompt="Try Streaming ? (Y/n) Defaulting in:", default="Y", timeout=5,
    )
    if not ans == "n":
        call(
            [
                "mpv",
                "--no-video",
                "--term-osd-bar",
                "--term-osd-bar-chars=[##-]",
                "--msg-level=all=error,statusline=status",
                item.enclosure_url,
            ],
        )
        return True
    # if file exist we check if filesize match with content length...
    print(f"File:    {newfilename}:")
    if os.path.isfile(newfilename):
        newfilelength = os.path.getsize(newfilename)
        try:
            validateFile(
                newfilename,
                item.time_published,
                item.enclosure_length,
                item.enclosure_url,
            )
        except (urllib.error.HTTPError, urllib.error.URLError):
            print("Connection when verifying existing file")
            return  # continue
        except socket.timeout:
            print("Connection timeout when downloading file")
            return  # continue

    # download or resume podcast. retry if timeout. cancel if error
    cancel_validate, newfilelength = try_download_item(
        newfilelength, newfilename, item,
    )

    if cancel_validate:
        return  # continue

    # validate downloaded file
    try:
        if validateFile(newfilename, 0, item.enclosure_length, item.enclosure_url):
            # set mtime if validated
            os.utime(newfilename, (newfilemtime, newfilemtime))
            print("File validated")

        elif newfilelength:
            # did not validate. see if we got same size as last time we
            # downloaded this file
            if newfilelength == os.path.getsize(newfilename):
                # ok, size is same. maybe data from response and rss is wrong.
                os.utime(newfilename, (newfilemtime, newfilemtime))
                print("File is assumed to be ok.")
    except urllib.error.HTTPError:
        print("Connection error when verifying download")
        return  # continue
    except socket.timeout:
        print("Connection timeout when downloading file")
        return  # continue

    call(
        [
            "mpv",
            "--no-video",
            "--term-osd-bar",
            "--term-osd-bar-chars=[##-]",
            "--msg-level=all=error,statusline=status",
            newfilename,
        ],
    )
    return True


def try_download_item(newfilelength, newfilename, item):
    """Try downloading item."""
    # download or resume podcast. retry if timeout. cancel if error
    retry_downloading = True
    while retry_downloading:
        retry_downloading = False
        cancel_validate = False
        try:
            if newfilelength:
                resumeDownloadFile(newfilename, item.enclosure_url)
            else:
                downloadFile(newfilename, item.enclosure_url)
        except (urllib.error.HTTPError, urllib.error.URLError):
            print("Connection error when downloading file")
            cancel_validate = True
        except socket.timeout:
            if newfilelength:
                if os.path.getsize(newfilename) > newfilelength:
                    print("Connection timeout. File partly resumed. Retrying")
                    retry_downloading = True
                    newfilelength = os.path.getsize(newfilename)
                else:
                    print("Connection timeout when resuming file")
                    cancel_validate = True
            else:
                if os.path.isfile(newfilename):
                    newfilelength = os.path.getsize(newfilename)
                    if newfilelength > 0:
                        print("Connection timeout. File partly downloaded. Retrying")
                        retry_downloading = True
                    else:
                        print("Connection timeout when downloading file")
                        cancel_validate = True
                else:
                    print("Connection timeout when downloading file")
                    cancel_validate = True

    return cancel_validate, newfilelength


def downloadFile(newfilename: str, enclosure_url: str) -> None:
    """Download File."""
    # create download dir path if it does not exist
    if not os.path.isdir(os.path.dirname(newfilename)):
        os.makedirs(os.path.dirname(newfilename))

    # download podcast
    print("Downloading ...")

    r = requests.get(enclosure_url, stream=True)
    # Total size in bytes.
    total_size = int(r.headers.get("content-length", 0))
    block_size = 1024  # 1 Kibibyte

    with tqdm.tqdm(total=total_size, unit="iB", unit_scale=True) as t:
        with open(newfilename, "wb") as out_file:
            for data in r.iter_content(block_size):
                t.update(len(data))
                out_file.write(data)
        if total_size != 0 and t.n != total_size:
            print("ERROR, something went wrong")

    """
    request = urllib.request.Request(enclosure_url)
    with urllib.request.urlopen(request, timeout=30) as response:
        total_size = int(response.info()["Content-Length"])
        block_size = 1024  # 1 Kibibyte
        with open(newfilename, "wb") as out_file:
            shutil.copyfileobj(response, out_file, 100 * 1024)
    """

    print("Download complete")


def resumeDownloadFile(newfilename: str, enclosure_url: str, headers: dict) -> None:
    """Resume file download."""
    # find start-bye and total byte-length
    print("Prepare resume")
    request = urllib.request.Request(enclosure_url, headers=headers)
    with urllib.request.urlopen(request) as response:
        info = response.info()
        if "Content-Length" in info:
            contentlength = int(info["Content-Length"])
        else:
            contentlength = -1

    if os.path.isfile(newfilename):
        start_byte = os.path.getsize(newfilename)
    else:
        start_byte = 0

    request = urllib.request.Request(enclosure_url, headers=headers)
    if start_byte > 0:
        if start_byte >= contentlength:
            print("Resume not possible. (startbyte greater then contentlength)")
            return
        request.add_header("Range", "bytes={start_byte}-")

    with urllib.request.urlopen(request, timeout=30) as response:
        with open(newfilename, "ab+") as out_file:

            info = response.info()
            out_file.seek(start_byte)

            if "Content-Range" in info:
                contentrange = info["Content-Range"].split(" ")[1].split("-")[0]
                if not int(contentrange) == start_byte:
                    print("Resume not possible. Cannot resume from byte {start_byte}")
                    return

            if not out_file.tell() == start_byte:
                print("Resume not possible. Cannot append data from byte {start_byte}")
                return

            print("Start resume from byte {start_byte}")
            print("Downloading ...")
            shutil.copyfileobj(response, out_file, 100 * 1024)

    print("Resume complete")


def validateFile(
    newfilename: str, time_published: int, enclosure_length: int, enclosure_url: str,
) -> bool:
    """Validate File."""
    if os.path.isfile(newfilename + ".err"):
        return True  # skip file

    # try to validate size

    filelength = os.path.getsize(newfilename)
    if enclosure_length:
        if abs(filelength - enclosure_length) <= 1:
            return True
    else:
        enclosure_length = 0

    request = urllib.request.Request(enclosure_url)
    with urllib.request.urlopen(request) as response:
        info = response.info()
        if "Content-MD5" in info:
            print(f"Content-MD5:{info['Content-MD5']}")

        if "Content-Length" in info:
            contentlength = int(info["Content-Length"])
            if abs(filelength - contentlength) <= 1:
                return True
            elif filelength > contentlength:
                return True

        print(
            "Filelength and content-length mismatch."
            f"filelength:{filelength}"
            f"enclosurelength:{enclosure_length}"
            f" contentlength:{int(info.get('Content-Length', '0'))}",
        )

        # if size validation fail, try to validate mtime.

        if time_published:
            filemtime = parseUnixTimeToDatetime(os.path.getmtime(newfilename))
            time_published = parseUnixTimeToDatetime(time_published)
            if time_published == filemtime:
                return True

            if "Last-Modified" in info:
                last_modified = parseRftTimeToDatetime(info["Last-Modified"])
                if last_modified == filemtime:
                    return True
            else:
                last_modified = ""

            print(
                f"Last-Modified mismatch."
                f" file-mtime:{filemtime}"
                f" Last-Modified:{last_modified}"
                f" pubdate:{time_published}",
            )
    return False


def getSafeFilenameFromText(text):
    """Get safe filename from text."""
    # remove reserved windows keywords
    reserved_win_keywords = r"(PRN|AUX|CLOCK\$|NUL|CON|COM[1-9]|LPT[1-9])"

    # remove reserved windows characters
    reserved_win_chars = '[\x00-\x1f\\\\?*:";|/<>]'
    # reserved posix is included in reserved_win_chars. reserved_posix_characters= '/\0'

    extra_chars = "[$@{}]"

    tmp = re.sub(
        "|".join((reserved_win_keywords, reserved_win_chars, extra_chars)), "", text,
    )
    return tmp


def parseFileExtensionFromUrl(enclosure_url):
    """File Extension Finder."""
    return os.path.splitext(enclosure_url)[1].split("?")[0].lower()


def parseRftTimeToDatetime(datetimestr: str) -> datetime.datetime:
    """Rft time to Date Time."""
    return email.utils.parsedate_to_datetime(datetimestr)


def parseUnixTimeToDatetime(datetimestamp: int) -> datetime.datetime:
    """Unix time to date time."""
    return datetime.datetime.fromtimestamp(datetimestamp)


if __name__ == "__main__":
    try:
        getpodcast()
    except KeyboardInterrupt:
        signal.alarm(0)
        print("\nExiting..")
        exit()
