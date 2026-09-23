#!/bin/bash
set -e

echo "=== Superhero Bot Deployment Script ==="

# Update system
echo "Updating system packages..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git

# Create user
echo "Creating superhero user..."
sudo useradd -m -s /bin/bash superhero || echo "User already exists"

# Clone repo
echo "Cloning repository..."
sudo mkdir -p /opt/superhero
sudo chown superhero:superhero /opt/superhero
sudo -u superhero git clone https://github.com/thezulkifil/superhero.git /opt/superhero

# Setup venv
echo "Setting up Python virtual environment..."
sudo -u superhero python3 -m venv /opt/superhero/venv
sudo -u superhero /opt/superhero/venv/bin/pip install --upgrade pip
sudo -u superhero /opt/superhero/venv/bin/pip install -r /opt/superhero/requirements.txt

# Create .env file
echo "Creating .env file..."
if [ ! -f /opt/superhero/.env ]; then
    sudo -u superhero cp /opt/superhero/.env.example /opt/superhero/.env
    echo "IMPORTANT: Edit /opt/superhero/.env with your Brevo credentials!"
    echo "Run: sudo nano /opt/superhero/.env"
else
    echo ".env file already exists, skipping..."
fi

# Install systemd service
echo "Installing systemd service..."
sudo cp /opt/superhero/superhero.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable superhero
sudo systemctl start superhero

echo ""
echo "=== Deployment Complete ==="
echo ""
echo "Next steps:"
echo "1. Edit your .env file: sudo nano /opt/superhero/.env"
echo "2. Restart the bot: sudo systemctl restart superhero"
echo "3. Check status: sudo systemctl status superhero"
echo "4. View logs: sudo journalctl -u superhero -f"
echo ""
