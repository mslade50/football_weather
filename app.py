import streamlit as st
from pages import nfl_weather, cfb_weather  # Import your page scripts as modules

# Set the app-wide configuration
st.set_page_config(page_title="Football Weather Dashboard", layout="wide")

# Define functions for each page
def nfl_weather_page():
    """Displays the NFL Weather Map page"""
    st.sidebar.markdown("# NFL Weather Map")
    nfl_weather.main()  # Assuming your 'nfl_weather.py' has a 'main' function

def cfb_weather_page():
    """Displays the College Football Weather Map page"""
    st.sidebar.markdown("# College Football Weather Map")
    cfb_weather.main()  # Assuming your 'cfb_weather.py' has a 'main' function

# Create a dictionary that maps page names to functions
page_names_to_funcs = {
    "NFL Weather Map": nfl_weather_page,
    "College Football Weather Map": cfb_weather_page
}

# Display the page selection in the sidebar
selected_page = st.sidebar.selectbox("Select a page", page_names_to_funcs.keys())

# Call the function corresponding to the selected page
page_names_to_funcs[selected_page]()
