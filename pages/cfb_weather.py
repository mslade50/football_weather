import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import datetime

st.set_page_config(layout="wide")

def load_data(filepath, **kwargs):
    return pd.read_excel(filepath, engine='openpyxl', **kwargs)

# Load the data
df_weather = load_data('cfb_weather.xlsx')  # First sheet (df_weather)
df_stadiums = load_data('cfb_weather_backtest.xlsx', sheet_name='Stadiums')
df_bt = load_data('cfb_weather_backtest.xlsx', sheet_name='Backtesting')


df_stadiums['Team'] = df_stadiums['Team'].replace('UConn', 'Connecticut')
df_stadiums['Team'] = df_stadiums['Team'].replace('FIU', 'Florida International')
# Create a new column 'home_tm' by extracting the team name after '@'
df_weather['home_tm'] = df_weather['Game'].apply(lambda x: x.split('@')[1].strip())
df = df_weather.merge(df_stadiums, left_on='home_tm', right_on='Team', how='left')

df[['lat', 'lon']] = df['game_loc'].str.split(',', expand=True)
df['lat'] = pd.to_numeric(df['lat'], errors='coerce')
df['lon'] = pd.to_numeric(df['lon'], errors='coerce')

def get_clv(open_value, current_value):
    return 'Positive' if open_value > current_value else 'Negative'
df = df.dropna(subset=['lat', 'lon'])
# Function to match a row in df to df_bt criteria and extract Sample, Margin, and ROI
def get_backtesting_data(row, df_bt):
    # Match temperature range
    temp_fg = row['temp_fg']
    wind_fg = row['wind_fg']
    open_val = row['Fd_open']
    current_val = row['FD_now']
    
    # Calculate the absolute value of the spread (Open)
    spread = abs(row['Open'])
    
    # Determine the CLV status
    clv_status = get_clv(open_val, current_val)
    df_bt['Wind Below'] = df_bt['Wind Below'].fillna(100)
    df_bt['Spread_l'] = df_bt['Spread_l'].fillna(0)
    df_bt['Temp Above'] = df_bt['Temp Above'].fillna(0)
    # Filter df_bt based on the criteria for temp, wind, and CLV
    match = df_bt[
        (df_bt['Temp Above'] <= temp_fg) & 
        (df_bt['Temp Below'] >= temp_fg) &
        (df_bt['Wind Above'] <= wind_fg) & 
        (df_bt['Wind Below'] >= wind_fg) &
        (df_bt['CLV from Open'] == clv_status)
    ]

    # Further filter based on spread, ensuring that spread is between Spread_l and Spread_h
    match = match[
        ((match['Spread_h'] >= spread) & (match['Spread_l'] <= spread))
    ]
    
    # If match is found, return the Sample, Margin, and ROI
    if not match.empty:
        return match.iloc[0]['Sample'], match.iloc[0]['Margin'], match.iloc[0]['ROI'],match.iloc[0]['Signal']
    else:
        return None, None, None, None  # No match found

# Apply the matching function to each row in df
df['Sample'], df['Margin'], df['ROI'],df['Signal'] = zip(*df.apply(lambda row: get_backtesting_data(row, df_bt), axis=1))


def assign_signal(row):
    # Get today's date and determine the day of the week (0 = Monday, 6 = Sunday)
    today = datetime.today()
    day_of_week = today.weekday()
    
    # Define the daily thresholds for Low Impact based on the day of the week
    low_impact_wind_thresholds = {
        0: 11.14,  # Monday
        1: 11.14,  # Tuesday
        2: 10.10,  # Wednesday
        3: 10.10,  # Thursday
        4: 9.31,   # Friday
        5: 8.79,   # Saturday
        6: 11.93   # Sunday
    }
    
    # Set the base threshold for Low Impact
    low_impact_wind_thresh = low_impact_wind_thresholds.get(day_of_week, 10)
    
    # Calculate thresholds for each impact level
    mid_impact_wind_thresh = low_impact_wind_thresh + 7.5
    high_impact_wind_thresh = low_impact_wind_thresh + 7.5
    very_high_impact_wind_thresh = low_impact_wind_thresh + 7.5

    # Define the impact signals based on updated criteria
    if row['wind_fg'] > very_high_impact_wind_thresh and row['temp_fg'] < 50 and -10.5 <= row['Open'] <= 10.5:
        return 'Very High Impact'
    elif row['wind_fg'] > high_impact_wind_thresh and row['temp_fg'] < 65 and -10.5 <= row['Open'] <= 10.5:
        return 'High Impact'
    elif ((row['wind_fg'] > mid_impact_wind_thresh and row['temp_fg'] < 65) or 
          (row['travel_alt'] > 800 and row['temp_fg'] > 75)) and -20.5 <= row['Open'] <= 20.5:
        return 'Mid Impact'
    elif ((row['wind_fg'] > low_impact_wind_thresh and row['temp_fg'] < 65) or 
          (row['rain_fg'] > 2) or 
          (row['temp_fg'] > 80 and row['home_temp'] < 57 and row['away_temp'] < 57)) and -20.5 <= row['Open'] <= 20.5:
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
    'Very High Impact':50,
    'No Impact': 7
})

# # Define opacity based on 'wind_impact'
# def assign_dot_opacity(row):
#     if row['wind_impact'] == 'High':
#         return 1.0
#     elif row['wind_impact'] == 'Low':
#         return 0.15
#     elif row['wind_impact'] == 'Med':
#         return 0.5
#     else:
#         return 1.0

# df['dot_opacity'] = df.apply(assign_dot_opacity, axis=1)

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
        "Record": True,    # Add Record
        "Percentage": True # Add Percentage
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
# fig.update_traces(
#     marker=dict(opacity=df['dot_opacity'])
# )

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
    "Current Spread: %{customdata[11]}<br>" +
    "Record: %{customdata[12]}<br>" +   # Add the Record column here
    "ROI: %{customdata[13]}<extra></extra>"  # Add the Percentage column here
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

filtered_df = df[df['ROI'].notna()]

# Keep only the specified columns
columns_to_keep = ['Game', 'Date', 'Time', 'temp_fg', 'wind_fg', 'Fd_open', 'FD_now', 'Open', 'Record', 'Percentage', 'Sample', 'Margin', 'ROI','Signal','game_loc']
filtered_df = filtered_df[columns_to_keep]
filtered_df['ROI']=filtered_df['ROI']*100
filtered_df['Percentage']= filtered_df['Percentage']*100

# Output the filtered DataFrame with the new columns
st.write(filtered_df)
