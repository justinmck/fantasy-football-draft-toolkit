# 🏈 Fantasy Football Draft Analysis (2024–2025)

This project analyzes the draft efficiency and season performance of teams in a 14-team fantasy football league using ESPN's API. It calculates custom metrics such as Value Over Replacement Player (VORP), draft delta, and performance payoff.

---

## 📦 Project Structure

```

├── notebooks/                # Jupyter notebooks for exploration
│   └── NB01.ipynb  # Data Collection
    └── NB02.ipynb  # Data Processing
    └── NB03.ipynb  # Data Analysis
│
├── scripts/                  # Python scripts for automation and data handling
│   └── utils.py              # Helper functions for data collection
│
├── data/                     # Data files
│   ├── raw/                  # Raw API data
│   └── fantasy_data.db           # Cleaned datasets
│
├── docs/                   
│   └── charts/ # Project charts
    └── tables/ # Project Tables
    └── index.qmd/ # Creates the website
Exported charts for visualization
│
├── README.md                 # Project overview
└── requirements.txt          # Python dependencies
```


---

## 🛠️ Setup Instructions

### 1. Clone the Repository

```bash
git clone https://github.com/yourusername/fantasy-draft-analysis.git
cd fantasy-draft-analysis
```
### 2. Create and Activate a Virtual Enviornment
```bash
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
```

### 3. Install Dependencies

Dependencies include:

- pandas
- requests
- seaborn
- matplotlib
- sqlalchemy
- ipykernel
- plotly

---
## ESPN API Access
To fetch data from ESPN's private league API, you'll need:

Your League ID

Your SWID and espn_s2 cookies from your ESPN account

### How to Get Your Credentials:
Go to your ESPN league in your browser.

Open Developer Tools (Right-click → Inspect → Application → Cookies).

Copy the values of SWID and espn_s2.

Then, update your .env file or pass these directly into your Python script where applicable:

python
Copy
Edit
from espn_api.football import League

league = League(
    league_id=12345678,
    year=2024,
    swid="{...}",
    espn_s2="AEB...longtoken..."
)

## 🚀 Reproducing the Analysis

Step-by-Step:
Step	File	Description
1️⃣	scripts/utils.py	Functions for fetching and processing raw league/player data
2️⃣	notebooks/draft_analysis.ipynb	Main notebook NB03: VORP calc, draft delta, visualizations
3️⃣	data/processed/	Stores cleaned and merged datasets
4️⃣	images/charts/	Output images for your report or website

You can run the entire analysis by starting with the notebooks and following cell-by-cell execution.

🧠 Key Metrics
VORP – Value Over Replacement Player, calculated per position

Draft Delta – Difference between Actual Pick and ADP

Boom/Bust – Difference between Actual and Projected points

Paid Off – Whether the player outperformed replacement value

## 👨‍💻 Author
Justin McKendry
University of Maryland — Computer Science & Finance
London School of Economics — Data Engineering (2025)

## 📎 License
MIT License (if you want this to be open source)

## 🙋‍♂️ Questions?
Reach out via [LinkedIn](https://linkedin.com/in/justinmckendry) or open an Issue in this repo.


---
