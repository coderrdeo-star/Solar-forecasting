import os
import sys
from ml_models import train_demo_models, POWER_MODEL_PATH, SEASON_MODEL_PATH

def main():
    print("=" * 60)
    print("SolarChip Demo Model Training")
    print("=" * 60)
    print("NOTE: Training models using synthetic physical simulation data...")

    results = train_demo_models()

    print(f"\n[OK] Hourly Power Model saved to: {POWER_MODEL_PATH}")
    print(f"[OK] Seasonal Model saved to:     {SEASON_MODEL_PATH}")
    print(f"Synthetic Validation MAE:        {results['demo_mae_w']} W")
    print("\n" + "!" * 60)
    print("CRITICAL NOTICE:")
    print("This MAE score is evaluated strictly on SYNTHETIC demonstration data.")
    print("It DOES NOT represent real-world physical solar forecasting accuracy.")
    print("Before using in production or making academic claims, train the model")
    print("on real time-series data measured from your physical solar installation")
    print("and a local weather station or pyranometer.")
    print("!" * 60 + "\n")

if __name__ == "__main__":
    main()
