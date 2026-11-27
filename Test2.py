
from pymongo import MongoClient
from datetime import datetime
import time

# MongoDB connection
client = MongoClient(
    "mongodb://localhost:27017/",
    serverSelectionTimeoutMS=30000,
    socketTimeoutMS=None
)

db = client["YOUR_DATABASE"]
col = db["SITcalculations_test"]

BATCH_SIZE = 500

total_modified = 0
batch_number = 0

pipeline = [
    {
        "$set": {
            "payload.sitCalcs": {
                "$let": {
                    "vars": {
                        "movedMeasurements": {
                            "$reduce": {
                                "input": {
                                    "$filter": {
                                        "input": "$payload.sitCalcs",
                                        "as": "c",
                                        "cond": {
                                            "$in": [
                                                "$$c.BA",
                                                ["CPLE", "CPLW"]
                                            ]
                                        }
                                    }
                                },
                                "initialValue": [],
                                "in": {
                                    "$concatArrays": [
                                        "$$value",
                                        {
                                            "$ifNull": [
                                                "$$this.Measurements",
                                                []
                                            ]
                                        }
                                    ]
                                }
                            }
                        },
                        "remainingCalcs": {
                            "$filter": {
                                "input": "$payload.sitCalcs",
                                "as": "c",
                                "cond": {
                                    "$not": [
                                        {
                                            "$in": [
                                                "$$c.BA",
                                                ["CPLE", "CPLW"]
                                            ]
                                        }
                                    ]
                                }
                            }
                        }
                    },
                    "in": {
                        "$map": {
                            "input": "$$remainingCalcs",
                            "as": "item",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {
                                                "$eq": [
                                                    "$$item.BA",
                                                    "DUK"
                                                ]
                                            },
                                            {
                                                "$eq": [
                                                    "$$item.RC",
                                                    "VACAR"
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        "$mergeObjects": [
                                            "$$item",
                                            {
                                                "Measurements": {
                                                    "$concatArrays": [
                                                        {
                                                            "$ifNull": [
                                                                "$$item.Measurements",
                                                                []
                                                            ]
                                                        },
                                                        "$$movedMeasurements"
                                                    ]
                                                }
                                            }
                                        ]
                                    },
                                    "$$item"
                                ]
                            }
                        }
                    }
                }
            }
        }
    }
]

try:

    start_time = time.time()

    while True:

        # Fetch matching document IDs
        batch_ids = [
            doc["_id"]
            for doc in col.find(
                {
                    "payload.sitCalcs.BA": {
                        "$in": ["CPLE", "CPLW"]
                    }
                },
                {"_id": 1}
            ).limit(BATCH_SIZE)
        ]

        if not batch_ids:
            break

        batch_number += 1

        # Update current batch
        result = col.update_many(
            {"_id": {"$in": batch_ids}},
            pipeline
        )

        total_modified += result.modified_count

        elapsed = round(time.time() - start_time, 2)

        print(
            f"Batch: {batch_number} | "
            f"Matched: {result.matched_count} | "
            f"Modified: {result.modified_count} | "
            f"Total Modified: {total_modified} | "
            f"Elapsed: {elapsed}s"
        )

    print("\nProcessing completed!")
    print("Total documents modified:", total_modified)
    print("Total batches:", batch_number)
    print("Total time:", round(time.time() - start_time, 2), "seconds")

except Exception as e:

    print("ERROR:", str(e))
    print("Documents modified before error:", total_modified)

finally:
    client.close()
  
