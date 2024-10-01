import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from streamlit_plotly_events import plotly_events
from datetime import datetime
import pytz

st.set_page_config(layout="wide")

# Load your Excel file
df = pd.read_excel('cfb_weather.xlsx', engine='openpyxl')
df[['lat', 'lon']] = df['game_loc'].str.split(',', expand=True)
df['lat'] = pd.to_numeric(df['lat'], errors='coerce')
df['lon'] = pd.to_numeric(df['lon'], errors='coerce')

# Process data for map
df['dot_size'] = df['gs_fg'].abs()*4+7  # Create dot size based on 'gs_fg'
df['Edge'] = (df['Edge'] * 100).round(2).astype(str) + '%'
df['My_total'] = (df['My_total'] * 4).round() / 4
# Update 'wind_vol' to 'Low' if 'wind_fg' is less than 11.99
df.loc[df['wind_fg'] < 11.99, 'wind_vol'] = 'Low'

# Assign dot color based on conditions
def assign_dot_color(row):
    if row['travel_alt'] > 1000:
        return 'saddlebrown'  # High altitude travel
    elif (row['temp_fg'] > 80 and row['wind_fg'] < 12) and (row['home_temp'] < 63.97 or row['away_temp'] < 63.97):
        if row['home_temp'] < 53.97 and row['away_temp'] < 53.97:
            return '#8B0000'  # Dark red for lower temperatures
        else:
            return 'red'  # Regular red
    elif row['temp_fg'] < 30 and row['wind_fg'] < 12:
        return 'blue'  # Cold
    elif row['wind_fg'] >= 12:
        return 'purple'  # Wind
    elif row['rain_fg'] > 0 and row['wind_fg'] < 12:
        return 'black'  # Rain
    else:
        return 'green' 

df['dot_color'] = df.apply(assign_dot_color, axis=1)
def assign_dot_opacity(row):
    # Check if the 'Game' column contains 'Colorado'
    if 'colorado' in row['Game'].lower():  # Convert to lowercase to avoid case sensitivity issues
        return 0.2  # Set opacity to 0.2 for games with 'Colorado'
    
    # Otherwise, apply the regular opacity rules based on 'dot_color' and 'wind_vol'
    if row['dot_color'] == 'purple':  # Only change opacity for 'Wind' dots
        if row['wind_vol'] == 'High':
            return 0.2  # Very low opacity for high wind
        elif row['wind_vol'] == 'Low':
            return 1.0  # Full opacity for low wind
        elif row['wind_vol'] == 'Mid':
            return 0.5  # Medium opacity for mid wind
        else:
            return 1.0  # Default opacity for undefined wind_vol
    else:
        return 1.0  # Full opacity for non-wind dots

# Apply the function to assign opacity
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
        "gs_fg": True,
        "Fd_open": True,
        "FD_now": True,
        "game_loc": True,
        "wind_diff": True,
        "wind_vol": True,
        "My_total": True,
        "Edge": True,
        "Open": True,
        "Current": True,
        "away_fg": True  # Added away_fg to hover data
    },
    size="dot_size",
    color="dot_color",
    color_discrete_map={
        '#8B0000': 'darkred',
        'red': 'red',
        'blue': 'blue',
        'purple': 'purple',
        'black': 'black',
        'green': 'green',
        'saddlebrown': 'saddlebrown',
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
# Manually update the legend labels for the colors
fig.for_each_trace(
    lambda t: t.update(
        name=t.name.replace('red', 'Heat')
                   .replace('blue', 'Cold')
                   .replace('purple', 'Wind')
                   .replace('black', 'Rain')
                   .replace('green', 'N/A')
                   .replace('#8B0000', 'Heat+')  # Dark red for "Heat+"
                   .replace('saddlebrown', 'Altitude')  # New replacement for gold
    )
)
fig.update_traces(marker=dict(sizemode='diameter', sizemin=1, sizeref=1))
# Apply opacity only to purple dots (Wind)
fig.update_traces(
    selector=dict(marker_color='purple'),  # Only select purple (wind) dots
    marker_opacity=df['dot_opacity']  # Apply opacity based on wind_vol
)
fig.update_traces(
    hovertemplate="<b>%{hovertext}</b><br>" + 
    "Wind: %{customdata[0]}<br>" +
    "Temp: %{customdata[1]}<br>" +
    "Rain: %{customdata[2]}<br>" +
    "Weather Impact: %{customdata[3]}%<br>" +
    "Open: %{customdata[4]}<br>" +
    "Current: %{customdata[5]}<br>" +
    "Game Location: %{customdata[6]}<br>" +
    "Wind Diff: %{customdata[7]}<br>" +
    "Wind Volatility: %{customdata[8]}<br>" +
    "My Total: %{customdata[9]}<br>" +
    "Edge: %{customdata[10]}<br>" +
    "Open Spread: %{customdata[11]}<br>" +
    "Current Spread: %{customdata[12]}<br>" +
    "Away Team Impact: %{customdata[13]}%<extra></extra>"  # Added away_fg to hover template
)

# Display in Streamlit with wide layout
st.title("College Football Weather Map")
if 'Timestamp' in df.columns:
    timestamp_str = df['Timestamp'].iloc[0]  # Get the timestamp string from the first row
    # Parse the timestamp string to a datetime object
    timestamp = datetime.fromisoformat(timestamp_str)
    # Format the timestamp
    formatted_timestamp = timestamp.strftime("%Y-%m-%d at %I:%M %p EST")
    st.subheader(f"Last updated: {formatted_timestamp}")
else:
    st.subheader("Timestamp not available")
# st.plotly_chart(fig, use_container_width=True)
st.plotly_chart(fig)

if st.sidebar.checkbox("Show game details", False):
    game = st.sidebar.selectbox("Select a game", df['Game'].unique())
    selected_game = df[df['Game'] == game]
    if not selected_game.empty:
        st.write(f"Details for {game}")

        # Rename columns first
        selected_game = selected_game.rename(columns={
            'home_temp': 'Home_t', 
            'away_temp': 'Away_t',
            'away_fg': 'Away tm',
            'game_loc': 'Game Location',
            'Total_open': 'Open',
            'Total_now': 'Current',
            'Under_open':'Price',
            'Under_now':'Price Now',
            'Spread_open': 'Open_s',
            'Spread_now': 'Current_s',
            'wind_fg': 'Wind',
            'temp_fg': 'Temp',
            'rain_fg': 'Rain',
            'wind_vol': 'Volatility',
            'wind_diff': 'Relative Wind',
            'year_built': 'Year',
            'wind_dir_fg': 'dir',
            'orient': 'O',
            'wind_impact': 'W_i',
            'weakest_wind_effect': 'Weak'
        })
        
        # Format columns with one decimal place
        columns_to_format = ['Away tm', 'Home_t', 'Away_t', 'Open', 'Current', 'Wind', 'My_total', 'Open_s', 'Current_s','Temp','Rain','Relative Wind']
        for col in columns_to_format:
            if col in ['Home_t', 'Away_t','Temp']:
                selected_game[col] = selected_game[col].apply(lambda x: f"{x:.1f}°")
            elif col == 'Away tm':
                selected_game[col] = selected_game[col].apply(lambda x: f"{x:.1f}%")
            else:
                selected_game[col] = selected_game[col].apply(lambda x: f"{x:.1f}")
        
        selected_game['Impact'] = selected_game['gs_fg'].apply(lambda x: f"{x:.1f}%")
        
        # Format Year as string without decimals
        selected_game['Year'] = selected_game['Year'].astype(int).astype(str)
        selected_game['O'] = selected_game['O'].astype(str)
        # Define column groups for each table
        weather_columns = ['Wind', 'Temp', 'Rain', 'Impact', 'Volatility', 'Relative Wind', 'Home_t', 'Away_t', 'Year']  # Add 'Year' to this list
        odds_columns = ['Open', 'Current', 'My_total', 'Edge', 'Open_s', 'Current_s', 'Away tm']
        game_info_columns = ['Date', 'Time','O','W_i','Weak','dir', 'Game Location']
        
        # Create a column layout
        col1, col2 = st.columns(2)
        
        # Display the three tables in the first column
        with col1:
            st.subheader("Weather Information")
            st.table(selected_game[weather_columns])
            
            st.subheader("Odds Information")
            st.table(selected_game[odds_columns])
            
            st.subheader("Game Information")
            st.table(selected_game[game_info_columns])
