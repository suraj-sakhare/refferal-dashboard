from flask import Flask, render_template, request
import requests

app = Flask(__name__)

@app.route('/', methods=['GET', 'POST'])
def dashboard():
    data = None
    if request.method == 'POST':
        # Get email from the form input
        email = request.form.get('email')
        api_url = "https://payppy.in/auth/referal-count-email"
        payload = {"email": email}
        
        try:
            # Fetch data from external API
            response = requests.post(api_url, json=payload)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as e:
            return f"Error fetching data: {e}"

    # Render the dashboard with data (if available)
    return render_template('dashboard.html', data=data)

if __name__ == "__main__":
    app.run(debug=True)
