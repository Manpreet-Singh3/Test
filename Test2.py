import time
import psycopg2
from pymongo import MongoClient, ASCENDING
from pymongo.errors import PyMongoError

# --- MongoDB Configuration ---
MONGO_URI = "mongodb://localhost:27017/"
MONGO_DB_NAME = "your_operational_db"  # <-- Match exact DB name in Studio 3T
GENERATION_COLLECTION = "generation_test_ba_move"
SIT_COLLECTION = "SITcalculations_test"
BATCH_SIZE = 500
TEMP_INDEX_NAME = "idx_sitCalcs_BA_temp"

# --- SITDataRecalc Constants (Verify against top of SITDataRecalc.py) ---
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


def load_generator_metadata(conn):
    """Loads generator mvaBase and inertia from PostgreSQL after updating BA to DUK."""
    pg_generators = {}
    with conn.cursor() as cur:
        # Adjust column names if your table uses different casing (e.g., mvabase, inertia)
        cur.execute("SELECT id, mvabase, inertia FROM public.generators WHERE rc = 'VACAR' AND ba = 'DUK';")
        for row in cur.fetchall():
            pg_generators[row[0]] = {
                "mvaBase": float(row[1] or 0.0),
                "inertia": float(row[2] or 0.0),
            }
    return pg_generators


def update_postgres():
    """Step 1: Delete CPLE/CPLW from public.bas, reassign generators to DUK, and return DUK metadata."""
    print("--- Step 1: Updating PostgreSQL ---")
    conn = psycopg2.connect(**PG_CONFIG)
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM public.bas WHERE rc = 'VACAR' AND name IN ('CPLE', 'CPLW');")
                print(f"Deleted {cur.rowcount} rows from public.bas")

                cur.execute("UPDATE public.generators SET ba = 'DUK' WHERE rc = 'VACAR' AND ba IN ('CPLE', 'CPLW');")
                print(f"Updated {cur.rowcount} rows in public.generators")

        pg_generators = load_generator_metadata(conn)
        print(f"Loaded {len(pg_generators)} VACAR|DUK generators from PostgreSQL.\n")
        return pg_generators
    finally:
        conn.close()


def update_generation_collection(db):
    """Step 2: Update generation measurements to DUK and remove old badatalinkstatus keys."""
    print(f"--- Step 2: Updating {GENERATION_COLLECTION} ---")
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


def compute_duk_from_measurements(measurements, pg_generators, gen_data=None):
    """Exact SITDataRecalc.py Compute() logic for VACAR|DUK using updated generation measurements."""
    gen_data = gen_data or {}
    mva_total = 0.0
    sir_total = 0.0
    duk_measurements = []

    for m in measurements:
        if m.get("RC") != "VACAR" or m.get("BA") != "DUK":
            continue

        mw = m.get("value", 0.0)
        mw1 = mw
        quality = m.get("Q", 0)
        gen_id = m.get("ID")

        if gen_id in gen_data:
            mvabase = float(gen_data[gen_id]["MVABase"])
            if mw < 0.5:
                mw, mw1, mvabase = 0.0, 0.0, 0.0
            if quality >= QUALITY_LIMIT:
                mw1, mvabase = 0.0, 0.0
            inertia = float(gen_data[gen_id]["Inertia"])
            si_base = float(mvabase) * inertia if mw1 is not None else 0.0
            mva_total += mvabase
            sir_total += si_base
            duk_measurements.append({"id": gen_id, "value": mw, "quality": quality})

        elif gen_id in pg_generators:
            mvabase = pg_generators[gen_id]["mvaBase"]
            inertia = pg_generators[gen_id]["inertia"]
            if mw < 0.5:
                mw, mw1, mvabase = 0.0, 0.0, 0.0
            if quality >= QUALITY_LIMIT:
                mw1, mvabase = 0.0, 0.0
            si_base = float(mvabase) * float(inertia) if mw1 is not None else 0.0
            mva_total += mw1
            sir_total += si_base
            duk_measurements.append({"id": gen_id, "value": mw, "quality": quality})

    rocof = 0.0 if sir_total == 0.0 else (CONTINGENCY / (2.0 * sir_total)) * 60.0
    freq_dev = round(rocof / 2.0, 3)
    time_to_ufls = 0.0 if rocof == 0.0 else round((60.0 - UFLS) / rocof, 4)
    ei_inertia = 0.0 if mva_total == 0.0 else round(sir_total / mva_total, 3)

    return {
        "BA": "DUK",
        "RC": "VACAR",
        "Type": 0,
        "MVA": round(mva_total, 3),
        "SIR": round(sir_total, 3),
        "ROCOF": round(rocof, 2),
        "FreqDev": freq_dev,
        "TimeToUFLS": time_to_ufls,
        "EIInertiaConstant": ei_inertia,
        "Measurements": duk_measurements,
    }


def recalculate_sit_collections(db, pg_generators, gen_data=None):
    """Step 3: Recalculate VACAR|DUK in SITcalculations_test using linked generation snapshotIDs."""
    print(f"--- Step 3: Recalculating {SIT_COLLECTION} from {GENERATION_COLLECTION} ---")
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
            snapshot_ids = [d.get("header", {}).get("snapshotID") for d in sit_docs if d.get("header", {}).get("snapshotID")]

            # Fetch linked generation documents in bulk for this batch
            gen_map = {
                g["header"]["snapshotID"]: g.get("payload", {}).get("measurements", [])
                for g in gen_col.find(
                    {"header.snapshotID": {"$in": snapshot_ids}},
                    {"header.snapshotID": 1, "payload.measurements": 1},
                )
            }

            batch_modified = 0
            for doc in sit_docs:
                snap_id = doc.get("header", {}).get("snapshotID")
                measurements = gen_map.get(snap_id)
                if not measurements:
                    continue

                new_duk_calc = compute_duk_from_measurements(measurements, pg_generators, gen_data)

                # Remove CPLE, CPLW, and old DUK (VACAR), then insert recalculated DUK
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
        print(f"Step 3 Complete -> Total Modified: {total_modified}\n")


def main():
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=30000, socketTimeoutMS=None)
    try:
        pg_generators = update_postgres()
        db = client[MONGO_DB_NAME]
        update_generation_collection(db)
        # Note: If you also have a static 'generatorData' dict/table in your app, pass it as gen_data
        recalculate_sit_collections(db, pg_generators, gen_data=None)
        print("All migration and SIT recalculation steps completed.")
    except (PyMongoError, psycopg2.Error) as err:
        print(f"Database error: {err}")
    finally:
        client.close()


if __name__ == "__main__":
    main()
    
