def calculate(rows, payload):
    policy=payload["policy"]; decisions=[]
    changes={c["case"]:c for c in payload.get("changes", [])}
    for row in rows:
        final_income=changes.get(row["case"], {}).get("income", row["income"]); limit=row["household"]*policy["income_limit_per_person"]; eligible=final_income <= limit
        decisions.append({"case":row["case"],"eligible":eligible,"income":final_income,"limit":limit,"benefit":policy["benefit"] if eligible else 0,"evidence":["application", "change"] if row["case"] in changes else ["application"]})
    return {"decisions":decisions,"eligible_count":sum(d["eligible"] for d in decisions)}
