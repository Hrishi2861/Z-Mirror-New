from asyncio import (
    sleep,
    gather
)

from bot import (
    Intervals,
    sabnzbd_client,
    nzb_jobs,
    nzb_listener_lock,
    task_dict_lock,
    LOGGER,
    bot_loop,
)
from bot.helper.ext_utils.bot_utils import new_task
from bot.helper.ext_utils.status_utils import getTaskByGid, speed_string_to_bytes
from bot.helper.ext_utils.task_manager import limit_checker, stop_duplicate_check
from bot.helper.telegram_helper.message_utils import auto_delete_message, delete_links


async def _remove_job(nzo_id, mid):
    res1, _ = await gather(
        sabnzbd_client.delete_history(
            nzo_id,
            delete_files=True
        ),
        sabnzbd_client.delete_category(f"{mid}"),
    )
    if not res1:
        await sabnzbd_client.delete_job(
            nzo_id,
            True
        )
    async with nzb_listener_lock:
        if nzo_id in nzb_jobs:
            del nzb_jobs[nzo_id]


@new_task
async def _onDownloadError(err, nzo_id, button=None):
    task = await getTaskByGid(nzo_id)
    await task.update() # type: ignore
    LOGGER.info(f"Cancelling Download: {task.name()}") # type: ignore
    await gather(
        task.listener.onDownloadError( # type: ignore
            err,
            button
        ),
        _remove_job(
            nzo_id,
            task.listener.mid # type: ignore
        ),
        sabnzbd_client.delete_job(
            nzo_id,
            delete_files=True
        ),
        sabnzbd_client.delete_category(f"{task.listener.mid}"), # type: ignore
    )
    async with nzb_listener_lock:
        if nzo_id in nzb_jobs:
            del nzb_jobs[nzo_id]


@new_task
async def _change_status(nzo_id, status):
    task = await getTaskByGid(nzo_id)
    async with task_dict_lock:
        task.cstatus = status # type: ignore


@new_task
async def _stop_duplicate(nzo_id):
    task = await getTaskByGid(nzo_id)
    await task.update() # type: ignore
    task.listener.name = task.name() # type: ignore
    (
        msg,
        button
    ) = await stop_duplicate_check(task.listener) # type: ignore
    if msg:
        _onDownloadError(
            msg,
            nzo_id,
            button
        ) # type: ignore


@new_task
async def _size_checker(nzo_id):
    task = await getTaskByGid(nzo_id)
    await task.update() # type: ignore
    task.listener.size = speed_string_to_bytes(task.size()) # type: ignore
    if limit_exceeded := await limit_checker(
        task.listener, # type: ignore
        isNzb=True
    ):
        LOGGER.info(
            f"NZB Limit Exceeded: {task.name()} | {task.size()}" # type: ignore
        )
        nmsg = _onDownloadError(
            limit_exceeded,
            nzo_id
        )
        await auto_delete_message(
            None,
            nmsg
        )


@new_task
async def _onDownloadComplete(nzo_id):
    task = await getTaskByGid(nzo_id)
    await task.listener.onDownloadComplete() # type: ignore
    if Intervals["stopAll"]:
        return
    await _remove_job(
        nzo_id,
        task.listener.mid # type: ignore
    )


async def _nzb_listener():
    while not Intervals["stopAll"]:
        async with nzb_listener_lock:
            try:
                jobs = (await sabnzbd_client.get_history())["history"]["slots"]
                downloads = (await sabnzbd_client.get_downloads())["queue"]["slots"]
                if len(nzb_jobs) == 0:
                    Intervals["nzb"] = ""
                    break
                for job in jobs:
                    nzo_id = job["nzo_id"]
                    if nzo_id not in nzb_jobs:
                        continue
                    if job["status"] == "Completed":
                        if not nzb_jobs[nzo_id]["uploaded"]:
                            nzb_jobs[nzo_id]["uploaded"] = True
                            _onDownloadComplete(nzo_id) # type: ignore
                            nzb_jobs[nzo_id]["status"] = "Completed"
                    elif job["status"] == "Failed":
                        _onDownloadError(
                            job["fail_message"],
                            nzo_id
                        ) # type: ignore
                    elif job["status"] in [
                        "QuickCheck",
                        "Verifying",
                        "Repairing",
                        "Fetching",
                        "Moving",
                        "Extracting",
                    ]:
                        if job["status"] != nzb_jobs[nzo_id]["status"]:
                            _change_status(
                                nzo_id,
                                job["status"]
                            ) # type: ignore
                for dl in downloads:
                    nzo_id = dl["nzo_id"]
                    if nzo_id not in nzb_jobs:
                        continue
                    if (
                        dl["status"] == "Downloading"
                        and not nzb_jobs[nzo_id]["stop_dup_check"]
                        and not dl["filename"].startswith("Trying")
                    ):
                        nzb_jobs[nzo_id]["stop_dup_check"] = True
                        _stop_duplicate(nzo_id) # type: ignore
                        _size_checker(nzo_id) # type: ignore
            except Exception as e:
                LOGGER.error(str(e))
        await sleep(3)


async def onDownloadStart(nzo_id):
    async with nzb_listener_lock:
        nzb_jobs[nzo_id] = {
            "uploaded": False,
            "stop_dup_check": False,
            "status": "Downloading",
        }
        if not Intervals["nzb"]:
            Intervals["nzb"] = bot_loop.create_task(_nzb_listener())
