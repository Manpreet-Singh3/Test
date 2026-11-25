var col = db.getCollection('SITcalculations_test');
var totalModified = 0;

while (true) {
  // 1. Fetch up to 500 matching _ids
  var batchIds = col.find(
    { "payload.sitCalcs.BA": { $in: ["CPLE", "CPLW"] } },
    { "_id": 1 }
  ).limit(500).toArray().map(function(doc) { return doc._id; });

  // Stop when no matching documents remain
  if (batchIds.length === 0) {
    break;
  }

  // 2. Update only this batch of 500 documents
  var res = col.updateMany(
    { "_id": { $in: batchIds } },
    [
      {
        $set: {
          "payload.sitCalcs": {
            $ifNull: [
              {
                $let: {
                  vars: {
                    movedMeasurements: {
                      $reduce: {
                        input: {
                          $filter: {
                            input: "$payload.sitCalcs",
                            as: "c",
                            cond: { $in: ["$$c.BA", ["CPLE", "CPLW"]] }
                          }
                        },
                        initialValue: [],
                        in: {
                          $concatArrays: [
                            "$$value",
                            { $ifNull: ["$$this.Measurements", []] }
                          ]
                        }
                      }
                    },
                    remainingCalcs: {
                      $filter: {
                        input: "$payload.sitCalcs",
                        as: "c",
                        cond: { $not: { $in: ["$$c.BA", ["CPLE", "CPLW"]] } }
                      }
                    }
                  },
                  in: {
                    $map: {
                      input: "$$remainingCalcs",
                      as: "item",
                      in: {
                        $cond: [
                          {
                            $and: [
                              { $eq: ["$$item.BA", "DUK"] },
                              { $eq: ["$$item.RC", "VACAR"] }
                            ]
                          },
                          {
                            $mergeObjects: [
                              "$$item",
                              {
                                Measurements: {
                                  $concatArrays: [
                                    { $ifNull: ["$$item.Measurements", []] },
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
              },
              "$payload.sitCalcs"
            ]
          }
        }
      }
    ]
  );

  totalModified += res.modifiedCount;
  print("SITcalculations updated so far: " + totalModified);
}

print("DONE! Total SITcalculations updated: " + totalModified);
