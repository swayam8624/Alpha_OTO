"""Terminal-only, zero-cloud comparison of completed research experiments."""
import json
import sys
for file in sys.argv[1:]:
    report = json.load(open(file))
    selection=report["selected_research_configuration"]
    test=report["holdout"]
    print("\n",report["symbol"],"-",file)
    print("Best validation config:",selection["model"],"horizon:",selection["horizon_bars"],
          "threshold:",selection["probability_threshold"])
    print("Shadow eligible:",report["shadow_candidate_only"])
    print("Validation return:",selection["compounded_validation_return"],
          "Validation trades:",selection["validation_trade_count"])
    print("Untouched diagnostic: net return",test["net_return"],
          "holdout trades",test["round_trips"],"baseline",test["benchmark"]["net_return"])
    print("AUC",test["auc"],"Brier",test["brier_score"],"status",report["status"])
    print("WARNING: Tested once; future changes require FRESH test data. No live orders.")
