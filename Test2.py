import time
import psycopg2
from pymongo import MongoClient, ASCENDING
from pymongo.errors import PyMongoError

# --- MongoDB Configuration ---
MONGO_URI = "mongodb://localhost:27017/"
MONGO_DB_NAME = "your_operational_db"       # <-- Match exact DB name in Studio 3T
GENERATION_COLLECTION = "generation_test_ba_move"
SIT_COLLECTION = "SITcalculations_test"
GENERATORS_COLLECTION = "Generators"        # <-- Used by SIT.py InitializeAreas()
BATCH_SIZE = 500
TEMP_INDEX_NAME = "idx_sitCalcs_BA_temp"

# --- SIT.py Constants (Verify against top of SIT.py / Parameters collection) ---
CONTINGENCY = 2750.0   # <-- Replace with exact 'contingency' value
UFLS = 59.5            # <-- Replace with exact 'ufls' value
QUALITY_LIMIT = 100    # <-- Replace with exact 'qualitylimit' value

# --- PostgreSQL Configuration ---
PG_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "dbname": "your_postgres_db",
    "user": "your_username",
    "password": "your_password",
}


def update_postgres():
    """Step 1: Delete CPLE/CPLW from public.bas and reassign generators to DUK in PostgreSQL."""
    print("--- Step 1: Updating PostgreSQL ---")
    conn = psycopg2.connect(**PG_CONFIG)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM public.bas WHERE rc = 'VACAR' AND name IN ('CPLE', 'CPLW');")
                print(f"Deleted {cur.rowcount} rows from public.bas")

                cur.execute("UPDATE public.generators SET ba = 'DUK' WHERE rc = 'VACAR' AND ba IN ('CPLE', 'CPLW');")
                print(f"Updated {cur.rowcount} rows in public.generators\n")
    finally:
        conn.close()


def update_and_load_mongo_generators(db):
    """Step 2: Update BA to DUK in MongoDB Generators collection and load 'areas' dict like SIT.py."""
    print(f"--- Step 2: Updating & Loading MongoDB {GENERATORS_COLLECTION} ---")
    gen_meta_col = db[GENERATORS_COLLECTION]

    res = gen_meta_col.update_many(
        {"rc": "VACAR", "ba": {"$in": ["CPLE", "CPLW"]}},
        {"$set": {"ba": "DUK"}},
    )
    print(f"Updated {res.modified_count} documents in MongoDB {GENERATORS_COLLECTION}.")

    areas = {}
    projection = {
        "_id": 0, "inertia": 1, "rc": 1, "ba": 1,
        "mvaBase": 1, "iccpId": 1, "psseBusName": 1, "machineId": 1,
    }
    for item in gen_meta_col.find({}, projection):
        iccp_id = item.get("iccpId")
        if iccp_id:
            areas[iccp_id] = {
                "Inertia": item.get("inertia", 0.0),
                "RC": item.get("rc"),
                "BA": item.get("ba"),
                "MVABase": item.get("mvaBase", 0.0),
                "psseBusName": item.get("psseBusName"),
                "machineId": item.get("machineId"),
            }
    print(f"Loaded {len(areas)} generator entries into areas lookup.\n")
    return areas


def update_generation_collection(db):
    """Step 3: Update generation measurements to DUK and remove old badatalinkstatus keys."""
    print(f"--- Step 3: Updating {GENERATION_COLLECTION} ---")
    col = db[GENERATION_COLLECTION]

    query = {
        "$or": [
            {"payload.measurements.BA": {"$in": ["CPLE", "CPLW"]}},
            {"payload.badatalinkstatus.VACAR|CPLE,Minneapolis": {"$exists": True}},
            {"payload.badatalinkstatus.VACAR|CPLE,Bloomington": {"$exists": True}},
            {"payload.badatalinkstatus.VACAR|CPLW,Minneapolis": {"$exists": True}},
            {"payload.badatalinkstatus.VACAR|CPLW,Bloomington": {"$exists": True}},
        ]
    }

    update = {
        "$set": {"payload.measurements.$[elem].BA": "DUK"},
        "$unset": {
            "payload.badatalinkstatus.VACAR|CPLE,Minneapolis": "",
            "payload.badatalinkstatus.VACAR|CPLE,Bloomington": "",
            "payload.badatalinkstatus.VACAR|CPLW,Minneapolis": "",
            "payload.badatalinkstatus.VACAR|CPLW,Bloomington": "",
        },
    }

    res = col.update_many(query, update, array_filters=[{"elem.BA": {"$in": ["CPLE", "CPLW"]}}])
    print(f"Generation Matched: {res.matched_count} | Modified: {res.modified_count}\n")


def compute_duk_sit_py(measurements, statuses, areas):
    """Exact SIT.py Compute() logic for VACAR|DUK."""
    mva_total = 0.0
    sir_total = 0.0
    duk_ids = []

    for m in measurements:
        if m.get("RC") != "VACAR" or m.get("BA") != "DUK":
            continue

        gen_id = m.get("ID")
        if gen_id not in areas:
            continue

        mw = m.get("value", 0.0)
        mw1 = mw
        mvabase_gen = areas[gen_id]["MVABase"]
        psse_bus_name = areas[gen_id]["psseBusName"]
        machine_id = areas[gen_id]["machineId"]
        mvabase = mvabase_gen

        if mw < 0.5:
            mw = 0
            mw1 = 0
            mvabase = 0

        quality = m.get("Q", 0)
        if quality >= QUALITY_LIMIT and quality < 128:
            mw1 = 0
            mvabase = 0

        inertia = areas[gen_id]["Inertia"]
        si_base = float(mvabase) * float(inertia) if mw1 is not None else 0

        mva_total += mvabase
        sir_total += si_base
        duk_ids.append({
            "id": gen_id,
            "value": mw,
            "quality": quality,
            "psseBusName": psse_bus_name,
            "machineId": machine_id,
            "mva": mvabase_gen,
            "inertia": inertia,
        })

    rocof_total = 0 if sir_total == 0.0 else (CONTINGENCY / (2.0 * sir_total)) * 60.0
    time_to_ufls = round((60.0 - UFLS) / rocof_total, 4) if rocof_total != 0 else 0
    ei_inertia = round(sir_total / mva_total, 3) if mva_total != 0 else 0.0

    # percentG calculation from statuses (Image 5, lines 339-346)
    try:
        mk = "VACAR|DUK,Minneapolis"
        bk = "VACAR|DUK,Bloomington"
        if statuses[mk]["percentG"] > statuses[bk]["percentG"]:
            percent_g = statuses[mk]["percentG"]
        else:
            percent_g = statuses[bk]["percentG"]
    except Exception:
        percent_g = 0

    return {
        "BA": "DUK",
        "RC": "VACAR",
        "Type": 0,
        "MVA": round(mva_total, 3),
        "SIR": round(sir_total, 3),
        "ROCOF": round(rocof_total, 2),
        "FreqDev": round(rocof_total / 2, 3),
        "TimeToUFLS": time_to_ufls,
        "EIInertiaConstant": ei_inertia,
        "percentG": percent_g,
        "Measurements": duk_ids,
    }


def recalculate_sit_collection(db, areas):
    """Step 4: Recalculate VACAR|DUK in SITcalculations_test via linked snapshotIDs."""
    print(f"--- Step 4: Recalculating {SIT_COLLECTION} ---")
    sit_col = db[SIT_COLLECTION]
    gen_col = db[GENERATION_COLLECTION]

    total_modified = 0
    batch_number = 0
    start_time = time.time()

    try:
        sit_col.create_index([("payload.sitCalcs.BA", ASCENDING)], name=TEMP_INDEX_NAME)
        gen_col.create_index([("header.snapshotID", ASCENDING)], background=True)

        while True:
            sit_docs = list(
                sit_col.find({"payload.sitCalcs.BA": {"$in": ["CPLE", "CPLW"]}}).limit(BATCH_SIZE)
            )
            if not sit_docs:
                break

            batch_number += 1
            snapshot_ids = [
                d.get("header", {}).get("snapshotID")
                for d in sit_docs
                if d.get("header", {}).get("snapshotID")
            ]

            gen_map = {
                g["header"]["snapshotID"]: g.get("payload", {})
                for g in gen_col.find(
                    {"header.snapshotID": {"$in": snapshot_ids}},
                    {"header.snapshotID": 1, "payload.measurements": 1, "payload.badatalinkstatus": 1},
                )
            }

            batch_modified = 0
            for doc in sit_docs:
                snap_id = doc.get("header", {}).get("snapshotID")
                gen_payload = gen_map.get(snap_id)
                if not gen_payload:
                    continue

                new_duk_calc = compute_duk_sit_py(
                    gen_payload.get("measurements", []),
                    gen_payload.get("badatalinkstatus", {}),
                    areas,
                )

                filtered_calcs = [
                    c for c in doc.get("payload", {}).get("sitCalcs", [])
                    if c.get("BA") not in ("CPLE", "CPLW")
                    and not (c.get("BA") == "DUK" and c.get("RC") == "VACAR")
                ]
                filtered_calcs.append(new_duk_calc)

                res = sit_col.update_one(
                    {"_id": doc["_id"]},
                    {"$set": {"payload.sitCalcs": filtered_calcs}},
                )
                batch_modified += res.modified_count

            total_modified += batch_modified
            print(f"Batch: {batch_number} | Modified: {batch_modified} | Total: {total_modified} | Time: {round(time.time() - start_time, 2)}s")

    finally:
        if TEMP_INDEX_NAME in sit_col.index_information():
            sit_col.drop_index(TEMP_INDEX_NAME)
        print(f"Step 4 Complete -> Total Modified: {total_modified}\n")


def main():
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=30000, socketTimeoutMS=None)
    try:
        update_postgres()
        db = client[MONGO_DB_NAME]
        areas = update_and_load_mongo_generators(db)
        update_generation_collection(db)
        recalculate_sit_collection(db, areas)
        print("All migration and SIT.py recalculation steps completed.")
    except (PyMongoError, psycopg2.Error) as err:
        print(f"Database error: {err}")
    finally:
        client.close()


if __name__ == "__main__":
    main()
    
