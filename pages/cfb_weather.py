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
df['dot_size'] = df['gs_fg'].abs()+0.8  # Create dot size based on 'gs_fg'
df['Edge'] = (df['Edge'] * 100).round(2).astype(str) + '%'
df['My_total'] = (df['My_total'] * 4).round() / 4
# Update 'wind_vol' to 'Low' if 'wind_fg' is less than 11.99
df.loc[df['wind_fg'] < 11.99, 'wind_vol'] = 'Low'

# Assign dot color based on conditions
def assign_dot_color(row):
    if row['temp_fg'] > 80 and row['wind_fg'] < 12:
        return 'red'  # Heat
    elif row['temp_fg'] < 30 and row['wind_fg'] < 12:
        return 'blue'  # Cold
    elif row['wind_fg'] >= 12:
        return 'purple'  # Wind
    elif row['rain_fg'] > 0 and row['wind_fg'] < 12:
        return 'black'  # Rain
    else:
        return 'green'  # Default/N/A

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
    lat="lat",  # Use the 'lat' column
    lon="lon",  # Use the 'lon' column
    hover_name="Game",  # Column to show on hover
    hover_data={
        "wind_fg": True,   # Show wind forecast
        "temp_fg": True,   # Show temperature forecast
        "rain_fg": True,
        "gs_fg": True,     # Show rain forecast
        "Fd_open": True,   # Show the opening FanDuel price
        "FD_now": True,    # Show the current FanDuel price
        "game_loc": True,  # Show game location
        "wind_diff": True, # Show wind difference
        "wind_vol": True,  # Show wind volatility
        "My_total": True,  # Add My_total to hover data
        "Edge": True,      # Add Edge to hover data
        "Open": True,      # Add Open spread to hover data
        "Current": True    # Add Current spread to hover data
    },
    size="dot_size",  # Use the 'gs_fg' field for dot size
    color="dot_color",  # Color based on conditions
    color_discrete_map={
        'red': 'red',
        'blue': 'blue',
        'purple': 'purple',
        'black': 'black',
        'green': 'green'
    },
    zoom=6,  # Adjusted for better zoom in the US
    height=1000,  # Make the map occupy a larger portion of the page
)

# Update the layout to focus on the US and adjust map display
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
    )
)

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
    "FD Open: %{customdata[4]}<br>" +
    "FD Now: %{customdata[5]}<br>" +
    "Game Location: %{customdata[6]}<br>" +
    "Wind Diff: %{customdata[7]}<br>" +
    "Wind Volatility: %{customdata[8]}<br>" +
    "My Total: %{customdata[9]}<br>" +
    "Edge: %{customdata[10]}<br>" +
    "Open Spread: %{customdata[11]}<br>" +
    "Current Spread: %{customdata[12]}<extra></extra>"
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
        
        selected_game['Impact'] = selected_game['gs_fg'].apply(lambda x: f"{x}%")
        
        # Format 'Away tm' with one decimal and percentage sign
        columns_to_format = ['Away tm', 'Home_t', 'Away_t', 'Open', 'Current', 'Wind', 'My_total', 'Open_s', 'Current_s','temp_fg']
        for col in columns_to_format:
            if col in ['Home_t', 'Away_t','temp_fg']:
                selected_game[col] = selected_game[col].apply(lambda x: f"{x:.1f}°")
            elif col == 'Away tm':
                selected_game[col] = selected_game[col].apply(lambda x: f"{x:.1f}%")
            else:
                selected_game[col] = selected_game[col].apply(lambda x: f"{x:.1f}")
        # Rename other columns as before
        selected_game = selected_game.rename(columns={
            'game_loc': 'Game Location',
            'Fd_open': 'Open',
            'FD_now': 'Current',
            'Open': 'Open_s',
            'Current': 'Current_s',
            'wind_fg': 'Wind',
            'temp_fg': 'Temp',
            'rain_fg': 'Rain',
            'wind_vol': 'Volatility',
            'wind_diff': 'Relative Wind'
        })
        
        reordered_columns = [
            'Wind', 
            'Temp', 
            'Rain',
            'Impact',
            'Volatility',
            'Open',
            'Current',
            'My_total', 
            'Edge', 
            'Open_s', 
            'Current_s',
            'Relative Wind',
            'Away tm',
            'Home_t',
            'Away_t',
            'Date',
            'Time',
            'Game Location'
        ]
        
        numeric_columns = [
            'Wind', 
            'Temp', 
            'Rain', 
            'Open',
            'Current',
            'My_total', 
            'Open_s', 
            'Current_s',
        ]
        
        selected_game[numeric_columns] = selected_game[numeric_columns].apply(lambda x: x.round(1))
        
        st.table(selected_game[reordered_columns])
