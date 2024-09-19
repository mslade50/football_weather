import streamlit as st

# Main page setup
st.set_page_config(layout="wide")

# Title for the main page
st.title("Football Weather Dashboard")

# Introduction or description
st.write("""
This dashboard is intended to provide an efficient way to spot potential weather impacts for FBS college football, and NFL football games. 

Use the sidebar to navigate between different pages, including:
- NFL Weather Map
- CFB Weather Map

The maps are intended to be as self explanatory as possible, but there are a few things that are worth pointing out. 

1. Wind is highly variable. If you are looking at totals early in the week, be careful using wind to inform your bets.
For this reason. I have added a filter to the "wind dots" which reduces the opacity for areas of the country that are notorious 
for higher wind volatility. If you "hover" over the dot, you will see a data point called "Wind_vol", this is what it is referring to.

2. The size of the dot corresponds to the perceived impact from weather. Bigger dot = larger weather impact = bigger edge towards the under.

3. The column "My_total" and "Edge" in CFB are speculative. I am working on creating fair values to apply the weather effects to, but 
I would not put my name behing them yet, so do not use those to inform your bets. 

4. Rain is important, but particularly variable in warm weather months, so I have overwrote the code to remove "rain" effects
until the month of September is over. That is why you will occassionally see dots color coded for rain, but they will be tiny until 
October arrives. 

5. The listed "lines" are not updating in real time, the openers are intended to be accurate, but if they seem to not make sense, it is likely
an error on my part, always double check. 

6. Game times are listed as the kickoff time in the local time zone. 
""")

# You can add more widgets or content to the main page if needed.
st.write("This main page will serve as the home for the football weather insights.")

# Optionally, you could add any useful widgets for the main dashboard here.
