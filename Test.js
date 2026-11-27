from pymongo import MongoClient
from pymongo.errors import PyMongoError

MONGO_URI = "mongodb://localhost:27017/"
DATABASE_NAME = "YourDatabase"

client = MongoClient(MONGO_URI)

try:
    db = client[DATABASE_NAME]
    collection = db["generation_test_ba_move"]

    # 1. Filter documents containing CPLE/CPLW
    #    in measurements OR badatalinkstatus

    query = {
        "$or": [
            {
                "payload.measurements.BA": {
                    "$in": ["CPLE", "CPLW"]
                }
            },
            {
                "payload.badatalinkstatus.VACAR|CPLE,Minneapolis": {
                    "$exists": True
                }
            },
            {
                "payload.badatalinkstatus.VACAR|CPLE,Bloomington": {
                    "$exists": True
                }
            },
            {
                "payload.badatalinkstatus.VACAR|CPLW,Minneapolis": {
                    "$exists": True
                }
            },
            {
                "payload.badatalinkstatus.VACAR|CPLW,Bloomington": {
                    "$exists": True
                }
            }
        ]
    }

    # 2. Update matching measurements and remove old keys

    update = {
        "$set": {
            "payload.measurements.$[elem].BA": "DUK"
        },
        "$unset": {
            "payload.badatalinkstatus.VACAR|CPLE,Minneapolis": "",
            "payload.badatalinkstatus.VACAR|CPLE,Bloomington": "",
            "payload.badatalinkstatus.VACAR|CPLW,Minneapolis": "",
            "payload.badatalinkstatus.VACAR|CPLW,Bloomington": ""
        }
    }

    # 3. Apply update only to matching array elements

    array_filters = [
        {
            "elem.BA": {
                "$in": ["CPLE", "CPLW"]
            }
        }
    ]

    # 4. Execute update

    result = collection.update_many(
        query,
        update,
        array_filters=array_filters
    )

    print("Update completed successfully")
    print(f"Matched documents: {result.matched_count}")
    print(f"Modified documents: {result.modified_count}")

except PyMongoError as ex:
    print(f"MongoDB error: {ex}")

finally:
    client.close()
