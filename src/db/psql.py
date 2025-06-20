import os
import time
from typing import Optional, List
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

import imgs
from conf import ks, envs
from util import log
from mod import models
from util.err import mkErr

lg = log.get(__name__)


def init():
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:
        register_heif_opener = None
        lg.info("pillow-heif not available, skipping HEIC/HEIF support")

    host = envs.psqlHost
    port = envs.psqlPort
    db = envs.psqlDb
    uid = envs.psqlUser

    if not all([host, port, db, uid]): raise RuntimeError("PostgreSQL connection settings not initialized.")

    try:
        with mkConn() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
        return True
    except Exception as e:
        lg.error(f"PostgreSQL connection test failed: {str(e)}")
        return False


@contextmanager
def mkConn():
    host = envs.psqlHost
    port = envs.psqlPort
    db = envs.psqlDb
    uid = envs.psqlUser
    pw = envs.psqlPass

    conn = None
    try:
        conn = psycopg.connect(
            host=host,
            port=port,
            dbname=db,
            user=uid,
            password=pw
        )
        yield conn
    except Exception: raise
    finally:
        if conn: conn.close()


def chk():
    try:
        with mkConn() as conn:
            with conn.cursor() as c:
                c.execute("SELECT 1")
        return True
    except Exception as e:
        raise mkErr(f"Failed to connect to PostgreSQL", e)


def fetchUser(usrId: str) -> Optional[models.Usr]:
    try:
        with mkConn() as conn:
            sql = """
            Select
                u.id,
                u.name,
                u.email
            From users u
            Where u.id = %s
            """
            with conn.cursor(row_factory=dict_row) as cursor:
                cursor.execute(sql, (usrId,))
                row = cursor.fetchone()

                if not row: raise RuntimeError( "no db row" )

                return models.Usr.fromDict(row)

    except Exception as e:
        raise mkErr(f"Failed to fetch userId[{usrId}]", e)

def fetchUsers() -> List[models.Usr]:
    try:
        with mkConn() as conn:
            sql = """
            Select
                u.id,
                u.name,
                u.email,
                a.key
            From users u
            Join api_keys a On u.id = a."userId"
            """
            with conn.cursor(row_factory=dict_row) as cursor:
                cursor.execute(sql)
                dics = cursor.fetchall()
                users = [models.Usr.fromDict(d) for d in dics]

                return users
    except Exception as e:
        raise mkErr(f"Failed to fetch users", e)


def count(usrId=None, assetType="IMAGE"):
    try:
        with mkConn() as conn:
            with conn.cursor() as cursor:
                #lg.info( f"[psql] count userId[{usrId}]" )

                # noinspection SqlConstantExpression
                sql = "Select Count(*) From assets Where 1=1"
                params = []

                if assetType:
                    sql += " AND type = %s"
                    params.append(assetType)

                if usrId:
                    sql += ' AND "ownerId" = %s'
                    params.append(usrId)

                sql += " AND status = 'active'"

                cursor.execute(sql, params)
                rst = cursor.fetchone()
                count = rst[0] if rst else 0

                #lg.info( f"[psql] count userId[{usrId}] rst[{count}]" )

                return count
    except Exception as e:
        raise mkErr(f"Failed to count assets", e)


#------------------------------------------------------
# 因為db裡的值會帶upload/ (經由web上傳的)
# 要對應到真實路徑, 必需要把upload替換為實際路徑
#------------------------------------------------------
def fixPrefix(path: Optional[str]):
    if path and path.startswith('upload/'): path = path[7:]
    return path


def testAssetsPath():
    try:
        with mkConn() as conn:
            sql = "Select path From asset_files Limit 5"
            with conn.cursor(row_factory=dict_row) as cursor:
                cursor.execute(sql)
                rows = cursor.fetchall()

                isOk = False

                # lg.info( f"[psql] test AccessPath.. fetched: {len(rows)}" )

                if not rows or not len(rows): return "No Assets"

                for row in rows:
                    path = row.get("path", "")
                    if path:
                        path = imgs.fixPath(fixPrefix(path))
                        isOk = os.path.exists(path)
                        # lg.info( f"[psql] test isOk[{isOk}] path: {path}" )
                        if isOk:
                            return "OK"

                    if not isOk: return f"{os.path.dirname(path)}"

                return f"test failed"

    except Exception as e:
        raise mkErr(f"Failed to test assets path", e)



def fetchAssets(usr: models.Usr, onUpdate: models.IFnProg):
    usrId = usr.id
    asType = "IMAGE"

    try:
        chk()

        onUpdate(11, f"start querying {usrId}")

        with mkConn() as conn:
            with conn.cursor(row_factory=dict_row) as cursor:
                # count all
                cntSql = "Select Count( * ) From assets Where status = 'active' And type = %s"
                cntArs = [asType]

                if usrId:
                    cntSql += " AND \"ownerId\" = %s"
                    cntArs.append(usrId)

                cursor.execute(cntSql, cntArs)
                rst = cursor.fetchone()
                if not rst: raise RuntimeError( "cannot count assets" )

                cntAll = rst.get("count", 0)

                if not cntAll: raise RuntimeError(f"fetch target type[{asType}] not found")

                # lg.info(f"Found {cntAll} {asType.lower()} assets...")
                onUpdate(15, f"start querying {cntAll}")

                #----------------------------------------------------------------
                # query assets
                #----------------------------------------------------------------
                sql = "Select * From assets Where status = 'active' And type = %s"

                params = [asType]

                if usrId:
                    sql += " AND \"ownerId\" = %s"
                    params.append(usrId)

                sql += " ORDER BY \"createdAt\" DESC"

                cursor.execute(sql, params)

                szBatch = 100
                szChunk = 100
                assets = []
                cntFetched = 0

                tStart = time.time()

                while True:
                    batch = cursor.fetchmany(szBatch)
                    if not batch: break

                    cntFetched += len(batch)

                    for row in batch: assets.append(row)

                    # if cntAll > 0:
                    #     if cntFetched > 0:
                    #         tmUsed = time.time() - tStart
                    #         tPerItem = tmUsed / cntFetched
                    #         remCnt = cntAll - cntFetched
                    #         remTime = tPerItem * remCnt
                    #         remMins = int(remTime / 60)
                    #         tmSuffix = f"remain: {remMins} mins"
                    #     else:
                    #         tmSuffix = "calcuating..."

                        # report("fetch", cntFetched, cntAll, f"processed {cntFetched}/{cntAll} ... {tmSuffix}")

                onUpdate(30, "main assets done... query for files..")

                #----------------------------------------------------------------
                # query asset files
                #----------------------------------------------------------------
                flsSql = """
                   Select "assetId", type, path
                   From asset_files
                   Where "assetId" = ANY(%s)
                """

                assetIds = [a['id'] for a in assets]
                afs = []

                for idx, i in enumerate(range(0, len(assetIds), szChunk)):
                    chunk = assetIds[i:i + szChunk]
                    cursor.execute(flsSql, (chunk,))
                    chunkRows = cursor.fetchall()
                    afs.extend(chunkRows)

                    chunkPct = min((idx + 1) * szChunk, len(assetIds))

                onUpdate(40, "files ready, combine data...")

                dictFiles = {}
                for af in afs:
                    assetId = af['assetId']
                    typ = af['type']
                    path = af['path']
                    if assetId not in dictFiles: dictFiles[assetId] = {}
                    dictFiles[assetId][typ] = path

                #----------------------------------------------------------------
                # query exif
                #----------------------------------------------------------------
                exifSql = """
                    Select *
                    From exif
                    Where "assetId" = ANY(%s)
                """

                exifData = {}
                for idx, i in enumerate(range(0, len(assetIds), szChunk)):
                    chunk = assetIds[i:i + szChunk]

                    cursor.execute(exifSql, (chunk,))
                    chunkRows = cursor.fetchall()

                    for row in chunkRows:
                        assetId = row['assetId']
                        exifItem = {}

                        for key, val in row.items():
                            if key == 'assetId': continue

                            if key in ('dateTimeOriginal', 'modifyDate') and val is not None:
                                if isinstance(val, str):
                                    exifItem[key] = val
                                else:
                                    exifItem[key] = val.isoformat() if val else None
                            elif val is not None:
                                exifItem[key] = val

                        if exifItem: exifData[assetId] = exifItem

                    chunkPct = min((idx + 1) * szChunk, len(assetIds))

                #----------------------------------------------------------------
                # query livephoto videos
                #----------------------------------------------------------------
                onUpdate(42, "query livephoto videos...")

                livePhotoSql = """
                    -- Method 1: Direct livePhotoVideoId
                    SELECT
                        a.id AS photo_id,
                        a."livePhotoVideoId" AS video_id,
                        v."encodedVideoPath" AS video_path,
                        v."originalPath" AS video_original_path
                    FROM assets a
                    JOIN assets v ON v.id = a."livePhotoVideoId" AND v.type = 'VIDEO'
                    WHERE a."livePhotoVideoId" IS NOT NULL
                    AND a.type = 'IMAGE'
                    AND a.id = ANY(%s)

                    UNION

                    -- Method 2: Match by livePhotoCID (for photos without livePhotoVideoId)
                    SELECT DISTINCT
                        a.id AS photo_id,
                        v.id AS video_id,
                        v."encodedVideoPath" AS video_path,
                        v."originalPath" AS video_original_path
                    FROM assets a
                    JOIN exif ae ON a.id = ae."assetId"
                    JOIN exif ve ON ae."livePhotoCID" = ve."livePhotoCID"
                    JOIN assets v ON ve."assetId" = v.id
                    WHERE ae."livePhotoCID" IS NOT NULL
                    AND a."livePhotoVideoId" IS NULL
                    AND a.type = 'IMAGE'
                    AND v.type = 'VIDEO'
                    AND v.id != a.id
                    AND a.id = ANY(%s)
                """

                livePaths = {}
                liveVdoIds = {}
                for idx, i in enumerate(range(0, len(assetIds), szChunk)):
                    chunk = assetIds[i:i + szChunk]
                    cursor.execute(livePhotoSql, (chunk, chunk))
                    chunkRows = cursor.fetchall()

                    for row in chunkRows:
                        photoId = row['photo_id']
                        videoId = row['video_id']
                        videoPath = row['video_path']
                        originalVideoPath = row['video_original_path']

                        finalPath = videoPath if videoPath else originalVideoPath
                        if finalPath:
                            livePaths[photoId] = fixPrefix(finalPath)
                            liveVdoIds[photoId] = videoId

                #----------------------------------------------------------------
                # combine & fetch thumbnail image
                #----------------------------------------------------------------
                onUpdate(45, "files ready, combine data...")

                cntOk = 0
                cntErr = 0

                rst = []

                for asset in assets:
                    assetId = asset['id']
                    if assetId in dictFiles:
                        for typ, path in dictFiles[assetId].items():
                            if typ == ks.db.thumbnail: asset['thumbnail_path'] = fixPrefix(path)
                            elif typ == ks.db.preview: asset['preview_path'] = fixPrefix(path)

                    asset['fullsize_path'] = fixPrefix(asset['originalPath'])

                    if assetId in livePaths: asset['video_path'] = livePaths[assetId]
                    if assetId in liveVdoIds: asset['video_id'] = liveVdoIds[assetId]

                    if assetId in exifData:
                        asset['exifInfo'] = exifData[assetId]
                    else:
                        lg.warn(f"[exif] NotFound.. assetId[{assetId}]")

                    # final check
                    pathThumbnail = asset.get('thumbnail_path')
                    if not pathThumbnail:
                        cntErr += 1
                        lg.warn(f"[psql] ignore asset: {asset}")
                        continue

                    cntOk += 1
                    rst.append(asset)

                    # if len(assets) > 0 and (cntOk % 100 == 0 or cntOk == len(assets)):
                    #     report("combine", cntOk, len(assets), f"processing {cntOk}/{len(assets)}...")

                lg.info(f"Successfully fetched {len(rst)} {asType.lower()} assets")
                onUpdate(5, f"Successfully fetched {len(rst)} {asType.lower()} assets")

                return rst
    except Exception as e:
        msg = f"Failed to FetchAssets: {str(e)}"
        raise mkErr(msg, e)
