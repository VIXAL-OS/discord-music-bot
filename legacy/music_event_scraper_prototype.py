import os
import json
import time
import logging
import re
import datetime
from typing import List, Dict, Any, Optional
import sqlite3
from dataclasses import dataclass

import requests
import schedule
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from bs4 import BeautifulSoup
from discord_webhook import DiscordWebhook, DiscordEmbed

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("music_events.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Load configuration
with open("config.json", "r") as f:
    config = json.load(f)

# Event data structure
@dataclass
class MusicEvent:
    """Represents a music event."""
    id: str  # Unique identifier for the event
    artist: str
    venue: str
    city: str
    date: datetime.datetime
    ticket_url: str
    price: Optional[str] = None
    genre: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    status: str = "pending"  # pending, approved, rejected
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert event to dictionary for storage."""
        return {
            "id": self.id,
            "artist": self.artist,
            "venue": self.venue,
            "city": self.city,
            "date": self.date.isoformat(),
            "ticket_url": self.ticket_url,
            "price": self.price,
            "genre": self.genre,
            "description": self.description,
            "image_url": self.image_url,
            "status": self.status
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'MusicEvent':
        """Create event from dictionary."""
        event_data = data.copy()
        event_data["date"] = datetime.datetime.fromisoformat(data["date"])
        return cls(**event_data)


# Database handler
class EventDatabase:
    """Handles storage and retrieval of event data."""
    
    def __init__(self, db_path: str = "events.db"):
        """Initialize database connection."""
        self.conn = sqlite3.connect(db_path)
        self.cursor = self.conn.cursor()
        self._create_tables()
    
    def _create_tables(self):
        """Create necessary tables if they don't exist."""
        self.cursor.execute('''
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            artist TEXT,
            venue TEXT,
            city TEXT,
            date TEXT,
            ticket_url TEXT,
            price TEXT,
            genre TEXT,
            description TEXT,
            image_url TEXT,
            status TEXT DEFAULT 'pending',
            posted INTEGER DEFAULT 0
        )
        ''')
        
        # Create genre_roles table to map genres to Discord roles
        self.cursor.execute('''
        CREATE TABLE IF NOT EXISTS genre_roles (
            genre TEXT PRIMARY KEY,
            role_id TEXT NOT NULL
        )
        ''')
        
        self.conn.commit()
    
    def add_event(self, event: MusicEvent) -> bool:
        """Add a new event to the database."""
        try:
            event_dict = event.to_dict()
            columns = ', '.join(event_dict.keys())
            placeholders = ', '.join(['?'] * len(event_dict))
            query = f"INSERT OR IGNORE INTO events ({columns}) VALUES ({placeholders})"
            
            self.cursor.execute(query, list(event_dict.values()))
            self.conn.commit()
            return self.cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error adding event to database: {e}")
            return False
    
    def get_pending_events(self) -> List[MusicEvent]:
        """Get events that are pending approval."""
        self.cursor.execute("SELECT * FROM events WHERE status = 'pending'")
        events = []
        for row in self.cursor.fetchall():
            event_dict = {
                "id": row[0],
                "artist": row[1],
                "venue": row[2],
                "city": row[3],
                "date": row[4],
                "ticket_url": row[5],
                "price": row[6],
                "genre": row[7],
                "description": row[8],
                "image_url": row[9],
                "status": row[10]
            }
            events.append(MusicEvent.from_dict(event_dict))
        return events
    
    def get_approved_unposted_events(self) -> List[MusicEvent]:
        """Get events that are approved but haven't been posted yet."""
        self.cursor.execute("SELECT * FROM events WHERE status = 'approved' AND posted = 0")
        events = []
        for row in self.cursor.fetchall():
            event_dict = {
                "id": row[0],
                "artist": row[1],
                "venue": row[2],
                "city": row[3],
                "date": row[4],
                "ticket_url": row[5],
                "price": row[6],
                "genre": row[7],
                "description": row[8],
                "image_url": row[9],
                "status": row[10]
            }
            events.append(MusicEvent.from_dict(event_dict))
        return events
    
    def get_event_by_id(self, event_id: str) -> Optional[MusicEvent]:
        """Get a single event by its ID."""
        self.cursor.execute("SELECT * FROM events WHERE id = ?", (event_id,))
        row = self.cursor.fetchone()
        if not row:
            return None
            
        event_dict = {
            "id": row[0],
            "artist": row[1],
            "venue": row[2],
            "city": row[3],
            "date": row[4],
            "ticket_url": row[5],
            "price": row[6],
            "genre": row[7],
            "description": row[8],
            "image_url": row[9],
            "status": row[10]
        }
        return MusicEvent.from_dict(event_dict)
    
    def update_event_field(self, event_id: str, field: str, value: str) -> bool:
        """Update a specific field for an event."""
        allowed_fields = ["artist", "venue", "city", "date", "ticket_url", 
                         "price", "genre", "description", "image_url"]
        
        if field not in allowed_fields:
            logger.error(f"Attempted to update invalid field: {field}")
            return False
        
        try:
            # Handle date field specially to ensure proper format
            if field == "date":
                try:
                    # Validate date format
                    datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    # Try to parse common date formats
                    date_formats = [
                        "%Y-%m-%d %H:%M:%S",
                        "%Y-%m-%d %H:%M",
                        "%Y-%m-%d",
                        "%m/%d/%Y %H:%M:%S",
                        "%m/%d/%Y %H:%M",
                        "%m/%d/%Y",
                        "%B %d, %Y %H:%M",
                        "%B %d, %Y",
                    ]
                    
                    parsed_date = None
                    for fmt in date_formats:
                        try:
                            parsed_date = datetime.datetime.strptime(value, fmt)
                            value = parsed_date.isoformat()
                            break
                        except ValueError:
                            continue
                    
                    if not parsed_date:
                        logger.error(f"Could not parse date: {value}")
                        return False
            
            # Update the field
            self.cursor.execute(f"UPDATE events SET {field} = ? WHERE id = ?", (value, event_id))
            self.conn.commit()
            return self.cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error updating event field: {e}")
            return False
    
    def update_event_status(self, event_id: str, status: str):
        """Update an event's status (pending, approved, rejected)."""
        self.cursor.execute("UPDATE events SET status = ? WHERE id = ?", (status, event_id))
        self.conn.commit()
    
    def mark_as_posted(self, event_id: str):
        """Mark an event as posted."""
        self.cursor.execute("UPDATE events SET posted = 1 WHERE id = ?", (event_id,))
        self.conn.commit()
    
    def add_genre_role(self, genre: str, role_id: str):
        """Add or update a genre to role mapping."""
        self.cursor.execute("INSERT OR REPLACE INTO genre_roles (genre, role_id) VALUES (?, ?)", 
                           (genre.lower(), role_id))
        self.conn.commit()
    
    def get_role_for_genre(self, genre: str) -> Optional[str]:
        """Get Discord role ID for a specific genre."""
        if not genre:
            return None
            
        self.cursor.execute("SELECT role_id FROM genre_roles WHERE genre = ?", (genre.lower(),))
        result = self.cursor.fetchone()
        return result[0] if result else None
    
    def close(self):
        """Close database connection."""
        self.conn.close()


# Music taste handler
class MusicTasteManager:
    """Manages user music preferences."""
    
    def __init__(self, spotify_config: Dict[str, str]):
        """Initialize with Spotify API configuration."""
        self.sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
            client_id=spotify_config["client_id"],
            client_secret=spotify_config["client_secret"],
            redirect_uri=spotify_config["redirect_uri"],
            scope="user-top-read user-library-read",
            cache_path=".spotify_cache"
        ))
        
        # Load manual preferences if available
        self.manual_artists = config.get("favorite_artists", [])
        self.manual_genres = config.get("favorite_genres", [])
    
    def get_favorite_artists(self, limit: int = 50) -> List[str]:
        """Get user's favorite artists from Spotify and manual input."""
        artists = self.manual_artists.copy()
        
        try:
            # Get top artists from Spotify
            top_artists = self.sp.current_user_top_artists(limit=limit, time_range="medium_term")
            for artist in top_artists["items"]:
                artists.append(artist["name"])
            
            # Get saved tracks' artists
            saved_tracks = self.sp.current_user_saved_tracks(limit=limit)
            for item in saved_tracks["items"]:
                for artist in item["track"]["artists"]:
                    artists.append(artist["name"])
            
            # Remove duplicates and return
            return list(set(artists))
        except Exception as e:
            logger.error(f"Error getting favorite artists from Spotify: {e}")
            return artists
    
    def get_favorite_genres(self) -> List[str]:
        """Get user's favorite genres from Spotify and manual input."""
        genres = self.manual_genres.copy()
        
        try:
            # Get genres from top artists
            top_artists = self.sp.current_user_top_artists(limit=50)
            for artist in top_artists["items"]:
                genres.extend(artist["genres"])
            
            # Remove duplicates and return
            return list(set(genres))
        except Exception as e:
            logger.error(f"Error getting favorite genres from Spotify: {e}")
            return genres


# Web scraper base class
class EventScraper:
    """Base class for event scrapers."""
    
    def __init__(self, user_location: str):
        """Initialize with user's location."""
        self.user_location = user_location
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
    
    def scrape_events(self) -> List[MusicEvent]:
        """Scrape events from the source."""
        raise NotImplementedError("Subclasses must implement this method")
    
    def _generate_event_id(self, artist: str, venue: str, date: datetime.datetime) -> str:
        """Generate a unique ID for an event."""
        date_str = date.strftime("%Y%m%d")
        combined = f"{artist}_{venue}_{date_str}".lower()
        # Remove special characters and spaces
        combined = re.sub(r'[^a-z0-9]', '', combined)
        return combined


# Example of a concrete scraper for Ticketmaster
class TicketmasterScraper(EventScraper):
    """Scraper for Ticketmaster events."""
    
    def __init__(self, user_location: str, api_key: str):
        """Initialize with user's location and Ticketmaster API key."""
        super().__init__(user_location)
        self.api_key = api_key
        self.base_url = "https://app.ticketmaster.com/discovery/v2/events.json"
    
    def scrape_events(self, artists: List[str], genres: List[str]) -> List[MusicEvent]:
        """Scrape events from Ticketmaster API matching user preferences."""
        events = []
        
        # Search for events by artist
        for artist in artists:
            try:
                params = {
                    "keyword": artist,
                    "classificationName": "music",
                    "city": self.user_location,
                    "apikey": self.api_key,
                    "size": 20
                }
                
                response = requests.get(self.base_url, params=params, headers=self.headers)
                data = response.json()
                
                if "_embedded" not in data:
                    continue
                
                for event in data["_embedded"]["events"]:
                    try:
                        # Extract event details
                        artist_name = event["name"]
                        venue = event["_embedded"]["venues"][0]["name"]
                        city = event["_embedded"]["venues"][0]["city"]["name"]
                        date_str = event["dates"]["start"]["dateTime"]
                        date = datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                        ticket_url = event["url"]
                        
                        # Optional fields
                        price = None
                        if "priceRanges" in event:
                            price = f"{event['priceRanges'][0]['min']} - {event['priceRanges'][0]['max']} {event['priceRanges'][0]['currency']}"
                        
                        genre = None
                        if "classifications" in event and len(event["classifications"]) > 0:
                            genre = event["classifications"][0]["genre"]["name"]
                        
                        image_url = None
                        if "images" in event and len(event["images"]) > 0:
                            image_url = event["images"][0]["url"]
                        
                        # Create event object
                        event_id = self._generate_event_id(artist_name, venue, date)
                        music_event = MusicEvent(
                            id=event_id,
                            artist=artist_name,
                            venue=venue,
                            city=city,
                            date=date,
                            ticket_url=ticket_url,
                            price=price,
                            genre=genre,
                            description=event.get("info"),
                            image_url=image_url
                        )
                        
                        events.append(music_event)
                    except Exception as e:
                        logger.error(f"Error processing Ticketmaster event: {e}")
                
                # Avoid rate limiting
                time.sleep(1)
                
            except Exception as e:
                logger.error(f"Error in Ticketmaster API request for {artist}: {e}")
        
        return events


# Bands In Town scraper
class BandsInTownScraper(EventScraper):
    """Scraper for Bands In Town events."""
    
    def __init__(self, user_location: str, app_id: str):
        """Initialize with user's location and app ID."""
        super().__init__(user_location)
        self.app_id = app_id
        self.base_url = "https://rest.bandsintown.com/artists"
    
    def scrape_events(self, artists: List[str]) -> List[MusicEvent]:
        """Scrape events from Bands In Town API matching user preferences."""
        events = []
        
        for artist in artists:
            try:
                # Get artist events
                encoded_artist = requests.utils.quote(artist)
                events_url = f"{self.base_url}/{encoded_artist}/events?app_id={self.app_id}"
                response = requests.get(events_url, headers=self.headers)
                
                if response.status_code != 200:
                    logger.warning(f"Failed to get events for {artist} from BandsInTown: {response.status_code}")
                    continue
                
                artist_events = response.json()
                
                for event in artist_events:
                    try:
                        artist_name = artist
                        venue = event["venue"]["name"]
                        city = f"{event['venue']['city']}, {event['venue']['region'] or event['venue']['country']}"
                        
                        # Parse date
                        date_str = event["datetime"]
                        date = datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                        
                        ticket_url = event["url"]
                        
                        # Optional fields
                        description = event.get("description", "")
                        
                        # Get lineup for genre hints
                        lineup = event.get("lineup", [])
                        genre = None  # BandsInTown doesn't provide genre directly
                        
                        # Get more artist info including image
                        artist_url = f"{self.base_url}/{encoded_artist}?app_id={self.app_id}"
                        artist_response = requests.get(artist_url, headers=self.headers)
                        
                        image_url = None
                        if artist_response.status_code == 200:
                            artist_data = artist_response.json()
                            image_url = artist_data.get("image_url")
                            
                            # Try to get genre from artist data
                            if "genres" in artist_data:
                                genre = artist_data["genres"][0] if artist_data["genres"] else None
                        
                        # Get offer information for pricing
                        offers = event.get("offers", [])
                        price = None
                        if offers:
                            price = f"{offers[0].get('status', 'Available')}"
                            if 'price' in offers[0]:
                                price = f"{offers[0]['price']} {offers[0].get('currency', 'USD')}"
                        
                        # Create event object
                        event_id = self._generate_event_id(artist_name, venue, date)
                        music_event = MusicEvent(
                            id=event_id,
                            artist=artist_name,
                            venue=venue,
                            city=city,
                            date=date,
                            ticket_url=ticket_url,
                            price=price,
                            genre=genre,
                            description=description,
                            image_url=image_url
                        )
                        
                        events.append(music_event)
                    
                    except Exception as e:
                        logger.error(f"Error processing BandsInTown event: {e}")
                
                # Avoid rate limiting
                time.sleep(1)
                
            except Exception as e:
                logger.error(f"Error in BandsInTown scraping for {artist}: {e}")
        
        return events


# Live Nation scraper
class LiveNationScraper(EventScraper):
    """Scraper for Live Nation events."""
    
    def __init__(self, user_location: str):
        """Initialize with user's location."""
        super().__init__(user_location)
        self.base_url = "https://www.livenation.com"
    
    def scrape_events(self, artists: List[str], genres: List[str] = None) -> List[MusicEvent]:
        """Scrape events from Live Nation matching user preferences."""
        events = []
        
        # First search by artist
        for artist in artists:
            try:
                # Search for artist
                search_url = f"{self.base_url}/search?q={artist.replace(' ', '+')}"
                response = requests.get(search_url, headers=self.headers)
                soup = BeautifulSoup(response.text, "html.parser")
                
                # Find event listings
                event_cards = soup.select(".event-card")
                
                for card in event_cards:
                    try:
                        # Check if this event matches our artist
                        title_elem = card.select_one(".event-title")
                        if title_elem and artist.lower() in title_elem.text.lower():
                            # Get event details
                            event_link = card.select_one("a.event-link")
                            if not event_link:
                                continue
                                
                            event_url = self.base_url + event_link["href"] if event_link["href"].startswith("/") else event_link["href"]
                            
                            # Visit event page
                            event_response = requests.get(event_url, headers=self.headers)
                            event_soup = BeautifulSoup(event_response.text, "html.parser")
                            
                            # Extract details
                            artist_name = title_elem.text.strip()
                            
                            date_elem = event_soup.select_one(".event-date")
                            date_str = date_elem.text.strip() if date_elem else ""
                            # Live Nation date format can vary, this is a simplified example
                            try:
                                # Try common formats
                                date = self._parse_date(date_str)
                            except:
                                # Default to today if parsing fails
                                logger.warning(f"Could not parse date: {date_str}")
                                date = datetime.datetime.now()
                            
                            venue_elem = event_soup.select_one(".venue-name")
                            venue = venue_elem.text.strip() if venue_elem else "Unknown Venue"
                            
                            city_elem = event_soup.select_one(".venue-location")
                            city = city_elem.text.strip() if city_elem else "Unknown Location"
                            
                            # Get ticket link
                            ticket_elem = event_soup.select_one("a.tickets-link")
                            ticket_url = ticket_elem["href"] if ticket_elem else event_url
                            
                            # Try to determine genre
                            genre_elem = event_soup.select_one(".event-genre")
                            genre = genre_elem.text.strip() if genre_elem else None
                            
                            # Try to find price
                            price_elem = event_soup.select_one(".ticket-price")
                            price = price_elem.text.strip() if price_elem else None
                            
                            # Get image
                            image_elem = event_soup.select_one(".event-image img")
                            image_url = image_elem["src"] if image_elem and "src" in image_elem.attrs else None
                            
                            # Description
                            desc_elem = event_soup.select_one(".event-description")
                            description = desc_elem.text.strip() if desc_elem else None
                            
                            # Create event object
                            event_id = self._generate_event_id(artist_name, venue, date)
                            music_event = MusicEvent(
                                id=event_id,
                                artist=artist_name,
                                venue=venue,
                                city=city,
                                date=date,
                                ticket_url=ticket_url,
                                price=price,
                                genre=genre,
                                description=description,
                                image_url=image_url
                            )
                            
                            events.append(music_event)
                            
                            # Avoid rate limiting
                            time.sleep(1)
                    
                    except Exception as e:
                        logger.error(f"Error processing Live Nation event card: {e}")
                
                # Avoid rate limiting
                time.sleep(2)
                
            except Exception as e:
                logger.error(f"Error in Live Nation scraping for {artist}: {e}")
        
        # Also check by genre if genres provided
        if genres:
            for genre in genres:
                try:
                    # Search for genre
                    genre_url = f"{self.base_url}/genres/{genre.replace(' ', '-').lower()}"
                    response = requests.get(genre_url, headers=self.headers)
                    
                    # Similar parsing as above, but for genre-based results
                    # Implementation omitted for brevity but would follow same pattern
                    
                except Exception as e:
                    logger.error(f"Error in Live Nation genre scraping for {genre}: {e}")
        
        return events
    
    def _parse_date(self, date_str: str) -> datetime.datetime:
        """Parse date string in various formats."""
        # Try several common date formats
        formats = [
            "%a %b %d %Y",  # Mon Jan 01 2023
            "%B %d, %Y",    # January 01, 2023
            "%m/%d/%Y",     # 01/01/2023
            "%Y-%m-%d"      # 2023-01-01
        ]
        
        # Extract just the date part if there's time included
        date_parts = date_str.split(' at ')
        date_part = date_parts[0].strip()
        
        # Try each format
        for fmt in formats:
            try:
                return datetime.datetime.strptime(date_part, fmt)
            except ValueError:
                continue
        
        # If all formats fail, raise exception
        raise ValueError(f"Could not parse date: {date_str}")


# OpusOne Productions scraper
class OpusOneScraper(EventScraper):
    """Scraper for OpusOne Productions events."""
    
    def __init__(self, user_location: str):
        """Initialize with user's location."""
        super().__init__(user_location)
        self.base_url = "https://www.opusoneproductions.com"  # Update with actual URL
    
    def scrape_events(self) -> List[MusicEvent]:
        """Scrape events from OpusOne Productions website."""
        events = []
        
        try:
            # Get events page
            events_url = f"{self.base_url}/events"
            response = requests.get(events_url, headers=self.headers)
            soup = BeautifulSoup(response.text, "html.parser")
            
            # Find event listings
            event_containers = soup.select(".event-item")  # Update with actual selector
            
            for container in event_containers:
                try:
                    # Extract event details
                    title_elem = container.select_one(".event-title")
                    artist_name = title_elem.text.strip() if title_elem else "Unknown Artist"
                    
                    date_elem = container.select_one(".event-date")
                    date_str = date_elem.text.strip() if date_elem else ""
                    # Parse date (format will depend on actual site structure)
                    try:
                        date = datetime.datetime.strptime(date_str, "%B %d, %Y")
                    except:
                        logger.warning(f"Could not parse OpusOne date: {date_str}")
                        date = datetime.datetime.now()
                    
                    venue_elem = container.select_one(".venue-name")
                    venue = venue_elem.text.strip() if venue_elem else "Unknown Venue"
                    
                    location_elem = container.select_one(".event-location")
                    city = location_elem.text.strip() if location_elem else self.user_location
                    
                    # Get detailed info
                    link_elem = container.select_one("a.event-link")
                    if link_elem:
                        event_url = self.base_url + link_elem["href"] if link_elem["href"].startswith("/") else link_elem["href"]
                        
                        # Visit event page
                        event_response = requests.get(event_url, headers=self.headers)
                        event_soup = BeautifulSoup(event_response.text, "html.parser")
                        
                        # Get more details
                        ticket_elem = event_soup.select_one("a.ticket-link")
                        ticket_url = ticket_elem["href"] if ticket_elem else event_url
                        
                        genre_elem = event_soup.select_one(".event-genre")
                        genre = genre_elem.text.strip() if genre_elem else None
                        
                        price_elem = event_soup.select_one(".ticket-price")
                        price = price_elem.text.strip() if price_elem else None
                        
                        image_elem = event_soup.select_one(".event-image img")
                        image_url = image_elem["src"] if image_elem and "src" in image_elem.attrs else None
                        
                        desc_elem = event_soup.select_one(".event-description")
                        description = desc_elem.text.strip() if desc_elem else None
                    else:
                        ticket_url = events_url
                        genre = None
                        price = None
                        image_url = None
                        description = None
                    
                    # Create event object
                    event_id = self._generate_event_id(artist_name, venue, date)
                    music_event = MusicEvent(
                        id=event_id,
                        artist=artist_name,
                        venue=venue,
                        city=city,
                        date=date,
                        ticket_url=ticket_url,
                        price=price,
                        genre=genre,
                        description=description,
                        image_url=image_url
                    )
                    
                    events.append(music_event)
                
                except Exception as e:
                    logger.error(f"Error processing OpusOne event: {e}")
            
        except Exception as e:
            logger.error(f"Error in OpusOne scraping: {e}")
        
        return events


# Facebook Events scraper
class FacebookEventsScraper(EventScraper):
    """Scraper for Facebook events the user is interested in."""
    
    def __init__(self, user_location: str, email: str, password: str, 
                 music_keywords: List[str] = None, headless: bool = True):
        """Initialize with Facebook credentials."""
        super().__init__(user_location)
        self.email = email
        self.password = password
        self.music_keywords = music_keywords or ["concert", "music", "band", "live", "show", "festival", "gig", "performance"]
        self.headless = headless
        
    def scrape_events(self) -> List[MusicEvent]:
        """Scrape events from Facebook that the user is interested in."""
        events = []
        browser = None
        
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.common.exceptions import TimeoutException, NoSuchElementException
            
            # Set up Chrome options
            chrome_options = Options()
            if self.headless:
                chrome_options.add_argument("--headless")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--disable-notifications")
            chrome_options.add_argument("--disable-infobars")
            chrome_options.add_argument("--disable-extensions")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument(f"user-agent={self.headers['User-Agent']}")
            
            # Initialize browser
            browser = webdriver.Chrome(options=chrome_options)
            wait = WebDriverWait(browser, 20)
            
            # Login to Facebook
            logger.info("Logging in to Facebook")
            browser.get("https://www.facebook.com/")
            
            # Accept cookies if prompted
            try:
                cookie_button = wait.until(EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Accept') or contains(text(), 'Allow')]")))
                cookie_button.click()
                time.sleep(2)
            except (TimeoutException, NoSuchElementException):
                pass  # No cookie dialog
            
            # Fill in login form
            try:
                email_field = wait.until(EC.presence_of_element_located((By.ID, "email")))
                password_field = browser.find_element(By.ID, "pass")
                login_button = browser.find_element(By.NAME, "login")
                
                email_field.send_keys(self.email)
                password_field.send_keys(self.password)
                login_button.click()
                
                # Wait for login to complete
                time.sleep(5)
                
                # Check if login was successful
                if "login" in browser.current_url or "checkpoint" in browser.current_url:
                    logger.error("Facebook login failed - additional verification may be required")
                    return events
                
                logger.info("Successfully logged in to Facebook")
            except Exception as e:
                logger.error(f"Error during Facebook login: {e}")
                return events
            
            # Navigate to Events page
            browser.get("https://www.facebook.com/events/")
            time.sleep(5)
            
            # Navigate to "Interested" events
            try:
                interested_tab = wait.until(EC.element_to_be_clickable((By.XPATH, "//span[contains(text(), 'Interested') or contains(text(), 'Going')]")))
                interested_tab.click()
                time.sleep(3)
                logger.info("Navigated to Interested events")
            except (TimeoutException, NoSuchElementException) as e:
                logger.error(f"Could not find Interested tab: {e}")
                
                # Try alternative method
                try:
                    browser.get("https://www.facebook.com/events/going")
                    time.sleep(5)
                    logger.info("Navigated to Going events directly")
                except Exception as ex:
                    logger.error(f"Could not navigate to events: {ex}")
                    return events
            
            # Scroll to load more events
            for _ in range(5):  # Scroll a few times to load more events
                browser.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(2)
            
            # Find event cards
            event_cards = browser.find_elements(By.XPATH, "//div[contains(@class, 'event')]//a[contains(@href, '/events/')]")
            event_links = [card.get_attribute("href") for card in event_cards if card.get_attribute("href")]
            
            # Filter out duplicates and non-event links
            event_links = [link for link in event_links if "/events/" in link]
            event_links = list(set(event_links))  # Remove duplicates
            
            logger.info(f"Found {len(event_links)} Facebook events to check")
            
            # Process each event
            for event_link in event_links:
                try:
                    browser.get(event_link)
                    time.sleep(3)
                    
                    # Get event details
                    try:
                        # Try to get the title
                        title_elem = wait.until(EC.presence_of_element_located((By.XPATH, "//h1 | //span[contains(@class, 'title')]")))
                        artist_name = title_elem.text.strip()
                        
                        # Check if this is a music event based on keywords
                        is_music_event = False
                        
                        # Check title
                        for keyword in self.music_keywords:
                            if keyword.lower() in artist_name.lower():
                                is_music_event = True
                                break
                        
                        # If not found in title, check description
                        if not is_music_event:
                            # Try to get description
                            try:
                                description_elem = browser.find_element(By.XPATH, "//div[contains(@class, 'description') or contains(@class, 'content')]")
                                description = description_elem.text.strip()
                                
                                for keyword in self.music_keywords:
                                    if keyword.lower() in description.lower():
                                        is_music_event = True
                                        break
                            except NoSuchElementException:
                                description = ""
                        
                        # Skip if not a music event
                        if not is_music_event:
                            continue
                        
                        # Get other event details
                        try:
                            date_elem = browser.find_element(By.XPATH, "//span[contains(@class, 'date') or contains(@class, 'time')] | //div[contains(text(), 'Date') or contains(text(), 'When')]/following-sibling::div")
                            date_str = date_elem.text.strip()
                            # Parse date (format varies widely on Facebook)
                            try:
                                # Try to find a date and time pattern
                                import re
                                from dateutil import parser
                                
                                # Extract date pattern
                                date_pattern = re.search(r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2}(?:,? \d{4})?(?:,? at)? \d{1,2}:\d{2}(?:AM|PM|am|pm)?', date_str)
                                if date_pattern:
                                    parsed_date = parser.parse(date_pattern.group(0))
                                else:
                                    parsed_date = parser.parse(date_str)
                                    
                                date = parsed_date
                            except Exception as e:
                                logger.warning(f"Could not parse Facebook date '{date_str}': {e}")
                                # Use a default date in the future
                                date = datetime.datetime.now() + datetime.timedelta(days=14)
                        except NoSuchElementException:
                            # Default to two weeks from now
                            date = datetime.datetime.now() + datetime.timedelta(days=14)
                            
                        # Get location/venue
                        try:
                            location_elem = browser.find_element(By.XPATH, "//div[contains(text(), 'Location') or contains(text(), 'Where')]/following-sibling::div | //span[contains(@class, 'location')]")
                            location = location_elem.text.strip()
                            
                            # Try to split into venue and city
                            location_parts = location.split(',', 1)
                            if len(location_parts) > 1:
                                venue = location_parts[0].strip()
                                city = location_parts[1].strip()
                            else:
                                venue = location
                                city = self.user_location
                        except NoSuchElementException:
                            venue = "Unknown Venue"
                            city = self.user_location
                            
                        # No direct price information usually available on Facebook
                        price = None
                        
                        # No reliable genre information on Facebook
                        genre = None
                        
                        # Get image URL if available
                        try:
                            image_elem = browser.find_element(By.XPATH, "//img[contains(@class, 'cover') or contains(@class, 'event')]")
                            image_url = image_elem.get_attribute("src")
                        except NoSuchElementException:
                            image_url = None
                        
                        # Get description if not already fetched
                        if 'description' not in locals() or not description:
                            try:
                                description_elem = browser.find_element(By.XPATH, "//div[contains(@class, 'description') or contains(@class, 'content')]")
                                description = description_elem.text.strip()
                            except NoSuchElementException:
                                description = ""
                        
                        # Create a unique ID
                        event_id = self._generate_event_id(artist_name, venue, date)
                        
                        # Create event object
                        music_event = MusicEvent(
                            id=event_id,
                            artist=artist_name,
                            venue=venue,
                            city=city,
                            date=date,
                            ticket_url=event_link,  # Use Facebook event link as ticket URL
                            price=price,
                            genre=genre,
                            description=description,
                            image_url=image_url
                        )
                        
                        events.append(music_event)
                        logger.info(f"Added Facebook event: {artist_name} at {venue}")
                        
                    except TimeoutException:
                        logger.warning(f"Timeout loading event details for {event_link}")
                        continue
                    
                except Exception as e:
                    logger.error(f"Error processing Facebook event {event_link}: {e}")
            
            logger.info(f"Scraped {len(events)} music events from Facebook")
            
        except Exception as e:
            logger.error(f"Error in Facebook scraping: {e}")
        
        finally:
            # Clean up
            if browser:
                try:
                    browser.quit()
                except:
                    pass
                
        return events


# Discord integration
class DiscordPoster:
    """Posts events to Discord."""
    
    def __init__(self, webhook_url: str, admin_webhook_url: str, bot_token: str, 
                 admin_user_id: str, bot_name: str = "Music Event Bot"):
        """Initialize with Discord webhook URL and bot token."""
        self.webhook_url = webhook_url
        self.admin_webhook_url = admin_webhook_url
        self.bot_token = bot_token
        self.admin_user_id = admin_user_id
        self.bot_name = bot_name
        self.db = None  # Will be set by main application
    
    def set_database(self, db: EventDatabase):
        """Set database reference for role lookups."""
        self.db = db
    
    def post_event(self, event: MusicEvent) -> bool:
        """Post an event to Discord public channel."""
        try:
            # Format date
            date_formatted = event.date.strftime("%A, %B %d, %Y at %I:%M %p")
            
            # Create webhook
            webhook = DiscordWebhook(url=self.webhook_url, username=self.bot_name)
            
            # Create embed
            embed = DiscordEmbed(
                title=f"{event.artist} at {event.venue}",
                description=event.description if event.description else "",
                color="3498db"  # Blue color
            )
            
            # Add fields
            embed.add_embed_field(name="Date", value=date_formatted)
            embed.add_embed_field(name="Location", value=f"{event.venue}, {event.city}")
            
            if event.price:
                embed.add_embed_field(name="Price", value=event.price)
            
            if event.genre:
                embed.add_embed_field(name="Genre", value=event.genre)
            
            # Add ticket link
            embed.add_embed_field(name="Tickets", value=f"[Buy Tickets]({event.ticket_url})")
            
            # Set timestamp
            embed.set_timestamp()
            
            # Set image if available
            if event.image_url:
                embed.set_image(url=event.image_url)
            
            # Add role mention if genre has associated role
            if self.db and event.genre:
                role_id = self.db.get_role_for_genre(event.genre)
                if role_id:
                    webhook.set_content(f"<@&{role_id}> New {event.genre} show alert!")
            
            # Add embed to webhook
            webhook.add_embed(embed)
            
            # Execute webhook
            response = webhook.execute()
            return response.status_code == 200
        
        except Exception as e:
            logger.error(f"Error posting event to Discord: {e}")
            return False
    
    def send_event_for_approval(self, event: MusicEvent) -> bool:
        """Send event to admin for approval via DM."""
        try:
            import discord
            from discord.ext import commands
            
            # This requires running a bot instance
            # For simplicity in this example, we'll use a webhook to admin channel
            # In a production environment, you'd want to use the Discord.py bot to DM the admin
            
            # Format date
            date_formatted = event.date.strftime("%A, %B %d, %Y at %I:%M %p")
            
            # Create webhook for admin channel
            webhook = DiscordWebhook(url=self.admin_webhook_url, username=f"{self.bot_name} - Approval")
            
            # Create embed
            embed = DiscordEmbed(
                title=f"APPROVAL NEEDED: {event.artist} at {event.venue}",
                description="Please review this event for accuracy before posting to the server.",
                color="f39c12"  # Orange color for pending approval
            )
            
            # Add fields
            embed.add_embed_field(name="Artist", value=event.artist)
            embed.add_embed_field(name="Date", value=date_formatted)
            embed.add_embed_field(name="Location", value=f"{event.venue}, {event.city}")
            
            if event.price:
                embed.add_embed_field(name="Price", value=event.price)
            
            if event.genre:
                embed.add_embed_field(name="Genre", value=event.genre)
                
                # Show what role would be mentioned
                if self.db:
                    role_id = self.db.get_role_for_genre(event.genre)
                    if role_id:
                        embed.add_embed_field(name="Role to mention", value=f"<@&{role_id}>")
            
            # Add description if available
            if event.description:
                embed.add_embed_field(name="Description", value=event.description[:1024], inline=False)
            
            # Add ticket link
            embed.add_embed_field(name="Tickets", value=f"[Buy Tickets]({event.ticket_url})")
            
            # Add ID for easier editing/commands
            embed.add_embed_field(name="Event ID", value=event.id)
            
            # Add approval/rejection/editing commands info
            embed.add_embed_field(
                name="Available Commands", 
                value=(
                    f"**Approval:**\n`!approve {event.id}`\n\n"
                    f"**Rejection:**\n`!reject {event.id}`\n\n"
                    f"**Editing:**\n`!edit {event.id} field value`\n"
                    f"Example: `!edit {event.id} genre indie rock`\n\n"
                    f"**View Help:**\n`!edithelp`"
                ), 
                inline=False
            )
            
            # Set timestamp
            embed.set_timestamp()
            
            # Set image if available
            if event.image_url:
                embed.set_image(url=event.image_url)
            
            # Add embed to webhook
            webhook.add_embed(embed)
            
            # Tag admin user
            webhook.set_content(f"<@{self.admin_user_id}> New event needs your approval!")
            
            # Execute webhook
            response = webhook.execute()
            return response.status_code == 200
            
        except Exception as e:
            logger.error(f"Error sending event for approval: {e}")
            return Falsed = admin_user_id
        self.bot_name = bot_name
        self.db = None  # Will be set by main application
    
    def set_database(self, db: EventDatabase):
        """Set database reference for role lookups."""
        self.db = db
    
    def post_event(self, event: MusicEvent) -> bool:
        """Post an event to Discord public channel."""
        try:
            # Format date
            date_formatted = event.date.strftime("%A, %B %d, %Y at %I:%M %p")
            
            # Create webhook
            webhook = DiscordWebhook(url=self.webhook_url, username=self.bot_name)
            
            # Create embed
            embed = DiscordEmbed(
                title=f"{event.artist} at {event.venue}",
                description=event.description if event.description else "",
                color="3498db"  # Blue color
            )
            
            # Add fields
            embed.add_embed_field(name="Date", value=date_formatted)
            embed.add_embed_field(name="Location", value=f"{event.venue}, {event.city}")
            
            if event.price:
                embed.add_embed_field(name="Price", value=event.price)
            
            if event.genre:
                embed.add_embed_field(name="Genre", value=event.genre)
            
            # Add ticket link
            embed.add_embed_field(name="Tickets", value=f"[Buy Tickets]({event.ticket_url})")
            
            # Set timestamp
            embed.set_timestamp()
            
            # Set image if available
            if event.image_url:
                embed.set_image(url=event.image_url)
            
            # Add role mention if genre has associated role
            if self.db and event.genre:
                role_id = self.db.get_role_for_genre(event.genre)
                if role_id:
                    webhook.set_content(f"<@&{role_id}> New {event.genre} show alert!")
            
            # Add embed to webhook
            webhook.add_embed(embed)
            
            # Execute webhook
            response = webhook.execute()
            return response.status_code == 200
        
        except Exception as e:
            logger.error(f"Error posting event to Discord: {e}")
            return False
    
    def send_event_for_approval(self, event: MusicEvent) -> bool:
        """Send event to admin for approval via DM."""
        try:
            import discord
            from discord.ext import commands
            
            # This requires running a bot instance
            # For simplicity in this example, we'll use a webhook to admin channel
            # In a production environment, you'd want to use the Discord.py bot to DM the admin
            
            # Format date
            date_formatted = event.date.strftime("%A, %B %d, %Y at %I:%M %p")
            
            # Create webhook for admin channel
            webhook = DiscordWebhook(url=self.admin_webhook_url, username=f"{self.bot_name} - Approval")
            
            # Create embed
            embed = DiscordEmbed(
                title=f"APPROVAL NEEDED: {event.artist} at {event.venue}",
                description="Please review this event for accuracy before posting to the server.",
                color="f39c12"  # Orange color for pending approval
            )
            
            # Add fields
            embed.add_embed_field(name="Artist", value=event.artist)
            embed.add_embed_field(name="Date", value=date_formatted)
            embed.add_embed_field(name="Location", value=f"{event.venue}, {event.city}")
            
            if event.price:
                embed.add_embed_field(name="Price", value=event.price)
            
            if event.genre:
                embed.add_embed_field(name="Genre", value=event.genre)
                
                # Show what role would be mentioned
                if self.db:
                    role_id = self.db.get_role_for_genre(event.genre)
                    if role_id:
                        embed.add_embed_field(name="Role to mention", value=f"<@&{role_id}>")
            
            # Add description if available
            if event.description:
                embed.add_embed_field(name="Description", value=event.description[:1024], inline=False)
            
            # Add ticket link
            embed.add_embed_field(name="Tickets", value=f"[Buy Tickets]({event.ticket_url})")
            
            # Add ID for easier editing/commands
            embed.add_embed_field(name="Event ID", value=event.id)
            
            # Add approval/rejection/editing commands info
            embed.add_embed_field(
                name="Available Commands", 
                value=(
                    f"**Approval:**\n`!approve {event.id}`\n\n"
                    f"**Rejection:**\n`!reject {event.id}`\n\n"
                    f"**Editing:**\n`!edit {event.id} field value`\n"
                    f"Example: `!edit {event.id} genre indie rock`\n\n"
                    f"**View Help:**\n`!edithelp`"
                ), 
                inline=False
            )
            
            # Set timestamp
            embed.set_timestamp()
            
            # Set image if available
            if event.image_url:
                embed.set_image(url=event.image_url)
            
            # Add embed to webhook
            webhook.add_embed(embed)
            
            # Tag admin user
            webhook.set_content(f"<@{self.admin_user_id}> New event needs your approval!")
            
            # Execute webhook
            response = webhook.execute()
            return response.status_code == 200
            
        except Exception as e:
            logger.error(f"Error sending event for approval: {e}")
            return False


# Discord bot for command handling
class DiscordBot:
    """Discord bot for handling admin approval commands."""
    
    def __init__(self, token: str, admin_user_id: str, db: EventDatabase, discord_poster: 'DiscordPoster'):
        """Initialize with bot token and admin user ID."""
        import discord
        from discord.ext import commands
        
        self.token = token
        self.admin_user_id = admin_user_id
        self.db = db
        self.discord_poster = discord_poster
        
        # Create bot instance
        intents = discord.Intents.default()
        intents.messages = True
        intents.message_content = True
        
        self.bot = commands.Bot(command_prefix='!', intents=intents)
        
        # Register commands
        @self.bot.command(name='approve')
        async def approve_event(ctx, event_id: str):
            # Check if user is admin
            if str(ctx.author.id) != self.admin_user_id:
                await ctx.send("You don't have permission to approve events.")
                return
            
            # Update event status in database
            self.db.update_event_status(event_id, 'approved')
            
            # Confirm to admin
            await ctx.send(f"Event {event_id} approved. It will be posted in the next cycle.")
        
        @self.bot.command(name='reject')
        async def reject_event(ctx, event_id: str):
            # Check if user is admin
            if str(ctx.author.id) != self.admin_user_id:
                await ctx.send("You don't have permission to reject events.")
                return
            
            # Update event status in database
            self.db.update_event_status(event_id, 'rejected')
            
            # Confirm to admin
            await ctx.send(f"Event {event_id} rejected. It will not be posted.")
        
        @self.bot.command(name='edit')
        async def edit_event(ctx, event_id: str, field: str, *, value: str):
            # Check if user is admin
            if str(ctx.author.id) != self.admin_user_id:
                await ctx.send("You don't have permission to edit events.")
                return
                
            # Validate field
            allowed_fields = ["artist", "venue", "city", "date", "ticket_url", 
                             "price", "genre", "description", "image_url"]
            
            if field not in allowed_fields:
                await ctx.send(f"Invalid field: {field}. Allowed fields are: {', '.join(allowed_fields)}")
                return
                
            # Get event to confirm it exists
            event = self.db.get_event_by_id(event_id)
            if not event:
                await ctx.send(f"Event with ID {event_id} not found.")
                return
                
            # Update the field
            success = self.db.update_event_field(event_id, field, value)
            if success:
                # Get updated event
                updated_event = self.db.get_event_by_id(event_id)
                
                # Show the updated event card
                await ctx.send(f"Updated {field} for event {event_id}. Here's the updated event:")
                
                # Create an embed to show the updated event
                embed = discord.Embed(
                    title=f"UPDATED: {updated_event.artist} at {updated_event.venue}",
                    description=updated_event.description if updated_event.description else "",
                    color=discord.Color.green()
                )
                
                # Format date
                date_formatted = updated_event.date.strftime("%A, %B %d, %Y at %I:%M %p")
                
                # Add fields
                embed.add_field(name="Artist", value=updated_event.artist)
                embed.add_field(name="Date", value=date_formatted)
                embed.add_field(name="Location", value=f"{updated_event.venue}, {updated_event.city}")
                
                if updated_event.price:
                    embed.add_field(name="Price", value=updated_event.price)
                
                if updated_event.genre:
                    embed.add_field(name="Genre", value=updated_event.genre)
                    
                    # Show what role would be mentioned
                    if self.db:
                        role_id = self.db.get_role_for_genre(updated_event.genre)
                        if role_id:
                            embed.add_field(name="Role to mention", value=f"<@&{role_id}>")
                
                # Add ticket link
                embed.add_field(name="Tickets", value=f"[Buy Tickets]({updated_event.ticket_url})")
                
                # Set image if available
                if updated_event.image_url:
                    embed.set_image(url=updated_event.image_url)
                
                # Add ID for reference
                embed.set_footer(text=f"Event ID: {updated_event.id}")
                
                await ctx.send(embed=embed)
            else:
                await ctx.send(f"Failed to update {field} for event {event_id}. Please check the value format.")
        
        @self.bot.command(name='show')
        async def show_event(ctx, event_id: str):
            # Check if user is admin
            if str(ctx.author.id) != self.admin_user_id:
                await ctx.send("You don't have permission to view events.")
                return
                
            # Get the event
            event = self.db.get_event_by_id(event_id)
            if not event:
                await ctx.send(f"Event with ID {event_id} not found.")
                return
                
            # Create an embed to show the event
            embed = discord.Embed(
                title=f"{event.artist} at {event.venue}",
                description=event.description if event.description else "",
                color=discord.Color.blue()
            )
            
            # Format date
            date_formatted = event.date.strftime("%A, %B %d, %Y at %I:%M %p")
            
            # Add fields
            embed.add_field(name="Artist", value=event.artist)
            embed.add_field(name="Date", value=date_formatted)
            embed.add_field(name="Location", value=f"{event.venue}, {event.city}")
            
            if event.price:
                embed.add_field(name="Price", value=event.price)
            
            if event.genre:
                embed.add_field(name="Genre", value=event.genre)
                
                # Show what role would be mentioned
                if self.db:
                    role_id = self.db.get_role_for_genre(event.genre)
                    if role_id:
                        embed.add_field(name="Role to mention", value=f"<@&{role_id}>")
            
            # Add ticket link
            embed.add_field(name="Tickets", value=f"[Buy Tickets]({event.ticket_url})")
            
            # Add status
            embed.add_field(name="Status", value=event.status)
            
            # Set image if available
            if event.image_url:
                embed.set_image(url=event.image_url)
            
            # Add ID for reference
            embed.set_footer(text=f"Event ID: {event.id}")
            
            await ctx.send(embed=embed)
        
        @self.bot.command(name='edithelp')
        async def edit_help(ctx):
            # Anyone can use this command
            help_text = """
**Event Editing Commands:**

`!show EVENT_ID` - Display all details for an event

`!edit EVENT_ID FIELD VALUE` - Edit a specific field of an event
Allowed fields:
- artist
- venue
- city
- date (formats: YYYY-MM-DD, MM/DD/YYYY, Month DD, YYYY)
- ticket_url
- price
- genre
- description
- image_url

Examples:
`!edit abc123 artist The National`
`!edit abc123 date 2023-05-15`
`!edit abc123 genre indie rock`
`!edit abc123 description This is a great show!`

`!approve EVENT_ID` - Approve an event for posting
`!reject EVENT_ID` - Reject an event (will not be posted)
            """
            
            embed = discord.Embed(
                title="Event Bot Commands Help",
                description=help_text,
                color=discord.Color.blue()
            )
            
            await ctx.send(embed=embed)
        
        @self.bot.command(name='setrole')
        async def set_genre_role(ctx, genre: str, role_id: str):
            # Check if user is admin
            if str(ctx.author.id) != self.admin_user_id:
                await ctx.send("You don't have permission to set genre roles.")
                return
            
            # Add genre-role mapping to database
            self.db.add_genre_role(genre, role_id)
            
            # Confirm to admin
            await ctx.send(f"Genre '{genre}' will now tag role <@&{role_id}>")
    
    def run(self):
        """Run the Discord bot."""
        self.bot.run(self.token)

# Main application
class MusicEventBot:
    """Main application to scrape and post music events."""
    
    def __init__(self, config_path: str = "config.json"):
        """Initialize with configuration file."""
        # Load configuration
        with open(config_path, "r") as f:
            self.config = json.load(f)
        
        # Initialize components
        self.db = EventDatabase(self.config.get("database_path", "events.db"))
        
        self.taste_manager = MusicTasteManager(self.config["spotify"])
        
        # Initialize scrapers
        self.scrapers = []
        
        # Ticketmaster
        if "ticketmaster" in self.config:
            self.scrapers.append(TicketmasterScraper(
                self.config["user_location"],
                self.config["ticketmaster"]["api_key"]
            ))
        
        # Bands In Town
        if "bandsintown" in self.config:
            self.scrapers.append(BandsInTownScraper(
                self.config["user_location"],
                self.config["bandsintown"]["app_id"]
            ))
        
        # Live Nation
        if "livenation" in self.config:
            self.scrapers.append(LiveNationScraper(
                self.config["user_location"]
            ))
        
        # OpusOne Productions
        if "opusone" in self.config:
            self.scrapers.append(OpusOneScraper(
                self.config["user_location"]
            ))
        
        # Facebook Events
        if "facebook" in self.config:
            self.scrapers.append(FacebookEventsScraper(
                self.config["user_location"],
                self.config["facebook"]["email"],
                self.config["facebook"]["password"],
                self.config["facebook"].get("music_keywords", None),
                self.config["facebook"].get("headless", True)
            ))
        
        # Instagram (if credentials are provided)
        if "instagram" in self.config:
            self.scrapers.append(InstagramScraper(
                self.config["user_location"],
                self.config["instagram"]["username"],
                self.config["instagram"]["password"]
            ))
        
        # Initialize Discord poster
        self.discord = DiscordPoster(
            self.config["discord"]["webhook_url"],
            self.config["discord"]["admin_webhook_url"],
            self.config["discord"]["bot_token"],
            self.config["discord"]["admin_user_id"],
            self.config["discord"].get("bot_name", "Music Event Bot")
        )
        
        # Connect database to discord poster for role lookups
        self.discord.set_database(self.db)
        
        # Initialize Discord bot in a separate thread if enabled
        if self.config["discord"].get("enable_bot", True):
            import threading
            self.discord_bot = DiscordBot(
                self.config["discord"]["bot_token"],
                self.config["discord"]["admin_user_id"],
                self.db,
                self.discord
            )
            
            # Start bot in a separate thread
            self.bot_thread = threading.Thread(target=self.discord_bot.run)
            self.bot_thread.daemon = True  # Thread will close when main program exits
            self.bot_thread.start()
    
    def scrape_all_sources(self):
        """Scrape all configured sources for events."""
        logger.info("Starting event scraping cycle")
        
        # Get user preferences
        favorite_artists = self.taste_manager.get_favorite_artists()
        favorite_genres = self.taste_manager.get_favorite_genres()
        
        logger.info(f"Found {len(favorite_artists)} favorite artists and {len(favorite_genres)} favorite genres")
        
        # Scrape events from all sources
        for scraper in self.scrapers:
            logger.info(f"Scraping events using {scraper.__class__.__name__}")
            
            try:
                # Different scrapers might have different parameter requirements
                if isinstance(scraper, TicketmasterScraper) or isinstance(scraper, LiveNationScraper):
                    events = scraper.scrape_events(favorite_artists, favorite_genres)
                elif isinstance(scraper, BandsInTownScraper):
                    events = scraper.scrape_events(favorite_artists)
                elif isinstance(scraper, OpusOneScraper) or isinstance(scraper, InstagramScraper):
                    events = scraper.scrape_events()
                else:
                    events = scraper.scrape_events(favorite_artists)
                
                logger.info(f"Found {len(events)} events from {scraper.__class__.__name__}")
                
                # Add events to database
                for event in events:
                    added = self.db.add_event(event)
                    if added:
                        logger.info(f"Added new event: {event.artist} at {event.venue} on {event.date}")
            
            except Exception as e:
                logger.error(f"Error in scraper {scraper.__class__.__name__}: {e}")
        
        # After scraping, send pending events for approval
        self.send_events_for_approval()
    
    def send_events_for_approval(self):
        """Send newly found events to admin for approval."""
        logger.info("Sending pending events for admin approval")
        
        # Get pending events
        events = self.db.get_pending_events()
        logger.info(f"Found {len(events)} pending events for approval")
        
        # Sort events by date (upcoming first)
        events.sort(key=lambda x: x.date)
        
        # Send events to admin for approval
        for event in events:
            try:
                # Only send future events
                if event.date > datetime.datetime.now():
                    success = self.discord.send_event_for_approval(event)
                    if success:
                        logger.info(f"Sent event for approval: {event.artist} at {event.venue}")
                    else:
                        logger.error(f"Failed to send event for approval: {event.artist} at {event.venue}")
                else:
                    # Mark past events as rejected without sending for approval
                    logger.info(f"Skipping past event: {event.artist} at {event.venue} on {event.date}")
                    self.db.update_event_status(event.id, 'rejected')
            
            except Exception as e:
                logger.error(f"Error sending event {event.id} for approval: {e}")
    
    def post_approved_events(self):
        """Post approved events to Discord."""
        logger.info("Posting approved events")
        
        # Get approved but unposted events
        events = self.db.get_approved_unposted_events()
        logger.info(f"Found {len(events)} approved events to post")
        
        # Sort events by date (upcoming first)
        events.sort(key=lambda x: x.date)
        
        # Post events to Discord
        for event in events:
            try:
                # Only post future events
                if event.date > datetime.datetime.now():
                    success = self.discord.post_event(event)
                    if success:
                        logger.info(f"Posted event to Discord: {event.artist} at {event.venue}")
                        self.db.mark_as_posted(event.id)
                    else:
                        logger.error(f"Failed to post event to Discord: {event.artist} at {event.venue}")
                else:
                    # Mark past events as posted without actually posting them
                    logger.info(f"Skipping past event: {event.artist} at {event.venue} on {event.date}")
                    self.db.mark_as_posted(event.id)
            
            except Exception as e:
                logger.error(f"Error posting event {event.id}: {e}")
    
    def run_scheduled(self):
        """Run the bot on a schedule."""
        # Schedule scraping (and sending for approval)
        schedule.every().day.at("03:00").do(self.scrape_all_sources)  # Run at 3 AM
        
        # Schedule posting of approved events
        schedule.every().day.at("09:00").do(self.post_approved_events)  # Run at 9 AM
        
        logger.info("Bot started with scheduled tasks")
        
        while True:
            schedule.run_pending()
            time.sleep(60)
    
    def run_once(self):
        """Run the bot once for testing."""
        logger.info("Running bot once for testing")
        self.scrape_all_sources()
        self.post_approved_events()
    
    def cleanup(self):
        """Clean up resources."""
        self.db.close()


# Example configuration file structure (config.json)
example_config = {
    "user_location": "New York",
    "database_path": "events.db",
    "favorite_artists": [
        "The National",
        "Arcade Fire",
        "Radiohead"
    ],
    "favorite_genres": [
        "indie rock",
        "alternative",
        "electronic"
    ],
    "spotify": {
        "client_id": "your_spotify_client_id",
        "client_secret": "your_spotify_client_secret",
        "redirect_uri": "http://localhost:8888/callback"
    },
    "ticketmaster": {
        "api_key": "your_ticketmaster_api_key"
    },
    "bandsintown": {
        "app_id": "your_bandsintown_app_id"
    },
    "livenation": {},
    "opusone": {},
    "facebook": {
        "email": "your_facebook_email",
        "password": "your_facebook_password",
        "headless": true,
        "music_keywords": [
            "concert", "music", "band", "live", "show", "festival", 
            "gig", "performance", "tour", "DJ"
        ]
    },
    "instagram": {
        "username": "your_instagram_username",
        "password": "your_instagram_password"
    },
    "discord": {
        "webhook_url": "https://discord.com/api/webhooks/your_webhook_url",
        "admin_webhook_url": "https://discord.com/api/webhooks/your_admin_webhook_url",
        "bot_token": "your_discord_bot_token",
        "admin_user_id": "your_discord_user_id",
        "bot_name": "Music Event Bot",
        "enable_bot": true,
        "genre_roles": {
            "indie rock": "role_id_for_indie_rock",
            "electronic": "role_id_for_electronic",
            "hip hop": "role_id_for_hip_hop"
        }
    }
}


# Genre role setup
def setup_genre_roles(bot: MusicEventBot):
    """Initialize genre roles from config."""
    if "genre_roles" in bot.config["discord"]:
        for genre, role_id in bot.config["discord"]["genre_roles"].items():
            bot.db.add_genre_role(genre, role_id)
            logger.info(f"Mapped genre '{genre}' to role ID '{role_id}'")

# Entry point
if __name__ == "__main__":
    # Check if config file exists, create example if not
    if not os.path.exists("config.json"):
        with open("config.json", "w") as f:
            json.dump(example_config, f, indent=4)
        print("Created example config.json file. Please update it with your details.")
        exit(0)
    
    # Create and run bot
    bot = MusicEventBot()
    
    # Set up genre roles from config
    setup_genre_roles(bot)
    
    # Command line arguments
    import sys
    if len(sys.argv) > 1:
        if sys.argv[1] == "test":
            bot.run_once()
        elif sys.argv[1] == "scrape":
            bot.scrape_all_sources()
        elif sys.argv[1] == "approve":
            bot.send_events_for_approval()
        elif sys.argv[1] == "post":
            bot.post_approved_events()
    else:
        try:
            bot.run_scheduled()
        except KeyboardInterrupt:
            print("Bot stopped by user")
        finally:
            bot.cleanup()
