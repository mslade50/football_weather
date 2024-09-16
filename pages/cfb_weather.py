import streamlit as st
import pandas as pd

# Load the weather data CSV file
@st.cache_data
def load_weather_data():
    df = pd.read_csv('cfb_locations_updated.csv')
    return df

# Load data
df = load_weather_data()

# Streamlit page configuration
st.title("College Football Weather Dashboard")
st.write("This dashboard visualizes weather data for College Football games.")

# Show a subset of the data for review
st.write(df.head())

# Filter the data (example: games in specific temperature ranges)
st.sidebar.title("Filter Options")
temp_filter = st.sidebar.slider("Select temperature range (°F)", min_value=int(df['temp_fg'].min()), max_value=int(df['temp_fg'].max()), value=(30, 80))
wind_filter = st.sidebar.slider("Select wind range (mph)", min_value=int(df['wind_fg'].min()), max_value=int(df['wind_fg'].max()), value=(0, 20))

# Apply the filters
filtered_df = df[(df['temp_fg'] >= temp_filter[0]) & (df['temp_fg'] <= temp_filter[1]) & 
                 (df['wind_fg'] >= wind_filter[0]) & (df['wind_fg'] <= wind_filter[1])]

st.write("Filtered Data:")
st.write(filtered_df)

# Basic map of game locations
st.map(filtered_df[['latitude', 'longitude']])

# Add custom visualizations and interactivity here (plots, graphs, etc.)
