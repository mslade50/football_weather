import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import datetime

st.set_page_config(layout="wide")

def load_data(filepath):
    return pd.read_excel(filepath, engine='openpyxl')

# Load the data
df = load_data('cfb_weather.xlsx')
df[['lat', 'lon']] = df['game_loc'].str.split(',', expand=True)
df['lat'] = pd.to_numeric(df['lat'], errors='coerce')
df['lon'] = pd.to_numeric(df['lon'], errors='coerce')

def assign_signal(row):
    # Get today's date and determine the day of the week (0 = Monday, 6 = Sunday)
    today = datetime.today()
    day_of_week = today.weekday()
    
    # Set the low impact threshold based on the day
    low_impact_wind_thresh = 8 if day_of_week == 4 or day_of_week == 5 else 10  # 4 = Friday, 5 = Saturday
    
    # Define the impact signals with the updated criteria
    if row['wind_fg'] > 15 and row['temp_fg'] < 50 and -10.5 <= row['Open'] <= 10.5:
        return 'Very High Impact'
    elif row['wind_fg'] > 15 and row['temp_fg'] < 70 and -10.5 <= row['Open'] <= 10.5:
        return 'High Impact'
    elif ((row['wind_fg'] > 15 and row['temp_fg'] < 75) or (row['travel_alt'] > 800 and row['temp_fg'] > 75)) and -20.5 <= row['Open'] <= 20.5:
        return 'Mid Impact'
    elif ((row['wind_fg'] > low_impact_wind_thresh and row['temp_fg'] < 75) or (row['rain_fg'] > 2) or (row['temp_fg'] > 80 and row['home_temp'] < 57 and row['away_temp'] < 57)) and -20.5 <= row['Open'] <= 20.5:
        return 'Low Impact'
    else:
        return 'No Impact'


    
# Assign signal and color, with special handling for rain and temperature on Low Impact
df['signal'] = df.apply(assign_signal, axis=1)
df['dot_color'] = df.apply(
    lambda row: 'black' if row['signal'] == 'Low Impact' and row['rain_fg'] > 2 else (
        'red' if row['signal'] == 'Low Impact' and row['temp_fg'] > 80 and row['home_temp'] < 57 and row['away_temp'] < 57 else (
            'blue' if row['signal'] == 'Low Impact' else (
                'orange' if row['signal'] == 'Mid Impact' else (
                    'purple' if row['signal'] == 'High Impact' else (
                        'darkred' if row['signal'] == 'Very High Impact' else 'green'
                    )
                )
            )
        )
    ), axis=1
)

# Assign dot sizes based on the signal
df['dot_size'] = df['signal'].map({
    'Low Impact': 15,
    'Mid Impact': 25,
    'High Impact': 40,
    'No Impact': 7
})

# Define opacity based on 'wind_impact'
def assign_dot_opacity(row):
    if row['wind_impact'] == 'High':
        return 1.0
    elif row['wind_impact'] == 'Low':
        return 0.15
    elif row['wind_impact'] == 'Med':
        return 0.5
    else:
        return 1.0

df['dot_opacity'] = df.apply(assign_dot_opacity, axis=1)

# Create the map using Plotly
fig = px.scatter_mapbox(
    df,
    lat="lat",
    lon="lon",
    hover_name="Game",
    hover_data={
        "wind_fg": True,
        "temp_fg": True,
        "rain_fg": True,
        "Fd_open": True,
        "FD_now": True,
        "game_loc": True,
        "Date": True,      # Add Game Date
        "Time": True,      # Add Game Time
        "wind_diff": True,
        "wind_vol": True,
        "Open": True,
        "Current": True,
    },
    size="dot_size",
    color="dot_color",
    color_discrete_map={
        'blue': 'blue',          # Low Impact (Wind)
        'orange': 'orange',      # Mid Impact
        'purple': 'purple',      # High Impact
        'darkred': 'darkred',    # Very High Impact
        'green': 'green',        # No Impact
        'black': 'black',        # Low Impact (Rain)
        'red': 'red'             # Low Impact (Temp)
    },
    zoom=6,
    height=1000,
)


fig.update_layout(
    mapbox_style="open-street-map",
    mapbox_center={"lat": 37.0902, "lon": -95.7129},  # Center the map in the U.S.
    mapbox_zoom=3.5,  # Zoom to focus on U.S. only
    legend_title_text='Weather Conditions',  # Set custom legend title
)
# Update the legend labels for the colors
fig.for_each_trace(
    lambda t: t.update(
        name=t.name.replace('blue', 'Low Impact (Wind)')
                   .replace('orange', 'Mid Impact')
                   .replace('purple', 'High Impact')
                   .replace('darkred', 'Very High Impact')
                   .replace('green', 'No Impact')
                   .replace('black', 'Low Impact (Rain)')
                   .replace('red', 'Low Impact (Temp)')
    )
)

fig.update_traces(marker=dict(sizemode='diameter', sizemin=1, sizeref=1))

# Apply opacity based on the wind impact level
fig.update_traces(
    marker=dict(opacity=df['dot_opacity'])
)

# Customize the hover template to exclude unwanted information
fig.update_traces(
    hovertemplate="<b>%{hovertext}</b><br>" +
    "Wind: %{customdata[0]} MPH<br>" +
    "Temp: %{customdata[1]}°F<br>" +
    "Rain: %{customdata[2]} in.<br>" +
    "Open: %{customdata[3]}<br>" +
    "Current: %{customdata[4]}<br>" +
    "Game Location: %{customdata[5]}<br>" +
    "Game Date: %{customdata[6]}<br>" +
    "Game Time: %{customdata[7]}<br>" +
    "Wind Diff: %{customdata[8]}<br>" +
    "Wind Volatility: %{customdata[9]}<br>" +
    "Open Spread: %{customdata[10]}<br>" +
    "Current Spread: %{customdata[11]}<extra></extra>"
)

# Display in Streamlit with wide layout
st.title("College Football Weather Map")
if 'Timestamp' in df.columns:
    timestamp_str = df['Timestamp'].iloc[0]  # Get the timestamp string from the first row
    timestamp = datetime.fromisoformat(timestamp_str)
    formatted_timestamp = timestamp.strftime("%Y-%m-%d at %I:%M %p EST")
    st.subheader(f"Last updated: {formatted_timestamp}")
else:
    st.subheader("Timestamp not available")
st.plotly_chart(fig)
if st.sidebar.checkbox("Show game details", False):
    game = st.sidebar.selectbox("Select a game", df['Game'].unique())
    selected_game = df[df['Game'] == game]
    if not selected_game.empty:
        st.write(f"Details for {game}")
        
        selected_game = selected_game.rename(columns={
            'home_temp': 'Home_t', 
            'away_temp': 'Away_t',
            'away_fg': 'Away tm',
            'game_loc': 'Game Location',
            'Fd_open': 'Open',
            'FD_now': 'Current',
            'Open': 'Open_s',
            'Current': 'Current_s',
            'wind_fg': 'Wind',
            'temp_fg': 'Temp',
            'rain_fg': 'Rain',
            'wind_vol': 'Volatility',
            'wind_diff': 'Relative Wind',
            'year_built': 'Year',
            'wind_dir_fg': 'Wind_dir',
            'orient': 'Orient',
            'wind_impact': 'Wind_imp',
            'weakest_wind_effect': 'Weakest_dir'
        })
        
        columns_to_format = ['Away tm', 'Home_t', 'Away_t', 'Open', 'Current', 'Wind', 'Open_s', 'Current_s','Temp','Rain','Relative Wind']
        for col in columns_to_format:
            if col in ['Home_t', 'Away_t','Temp']:
                selected_game[col] = selected_game[col].apply(lambda x: f"{x:.1f}°")
            elif col == 'Away tm':
                selected_game[col] = selected_game[col].apply(lambda x: f"{x:.1f}%")
            else:
                selected_game[col] = selected_game[col].apply(lambda x: f"{x:.1f}")
        
        selected_game['Impact'] = selected_game['gs_fg'].apply(lambda x: f"{x:.1f}%")
        
        selected_game['Year'] = selected_game['Year'].astype(int).astype(str)
        
        weather_columns = ['Wind', 'Temp', 'Rain', 'Volatility', 'Relative Wind', 'Home_t', 'Away_t', 'Year']
        odds_columns = ['Open', 'Current', 'Open_s', 'Current_s', 'Away tm']
        game_info_columns = ['Date', 'Time','Orient','Wind_dir','Wind_imp','Weakest_dir', 'Game Location']
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader("Weather Information")
            st.table(selected_game[weather_columns])
            
            st.subheader("Odds Information")
            st.table(selected_game[odds_columns])
            
            st.subheader("Game Information")
            st.table(selected_game[game_info_columns])
