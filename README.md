# Kubling SQLAlchemy Dialect

[![Kubling license](https://img.shields.io/badge/license-Apache%202.0-blue.svg?style=flat-square)](LICENSE)
![PyPI](https://img.shields.io/pypi/v/kubling-sqlalchemy?style=flat-square)
![Contributions Welcome](https://img.shields.io/badge/contributions-welcome-brightgreen?style=flat-square)

A custom SQLAlchemy dialect for integrating Kubling with [Apache Superset](https://superset.apache.org/). 
This dialect enables seamless querying and visualization of Kubling data within Superset by leveraging its PostgreSQL-compatible protocol.

---

## 🚀 Features

- **PostgreSQL-Compatible**: Connects to Kubling using its PostgreSQL protocol.
- **Data Type Mapping**: Automatically maps Kubling data types to SQLAlchemy types.
- **Seamless Integration**: Fully supports Superset for visualizations and dashboards.
- **Open Source**: Contributions and feedback are welcome to improve this dialect!

---

## 🛠️ Installation

### Prerequisites

- Python 3.7+
- Apache Superset
- Access to a Kubling instance

### Install the Dialect

You can install the dialect using `pip`:

```bash
pip install kubling-sqlalchemy
```

### **Build**

The build process for this project is divided into two pipelines:

1. **Main Pipeline**:
   - Responsible for building the Kubling SQLAlchemy dialect.
   - Pushes the library to [PyPI](https://pypi.org/) for distribution.

2. **Superset Pipeline**:
   - Located in the `superset/` directory.
   - Builds an OCI-compliant image based on the official Apache Superset image.
   - Installs the Kubling dialect into the Superset container, enabling seamless Kubling integration.

---

### **CI/CD and Docker Context**

- This project still utilizes **Jenkins** for continuous integration and delivery, including both pipelines.
- **Dockerized Build Process**:
  - All build and packaging tasks occur within a Docker context.
  - This design ensures ease of forking and customization, allowing you to adapt the pipelines to your specific needs with minimal effort.

---

### Configure Apache Superset

#### **Using the Kubling Dialect**

1. Add the dialect library to your Superset installation:

   ```bash
   pip install kubling-sqlalchemy
   ```

2. Restart Superset to apply the changes:

   ```bash
   superset run -p 8088
   ```

3. Add a new database connection in Superset using the following connection string format:

   ```
   kubling://<username>:<password>@<host>:<port>/<vdb_name>
   ```

---

#### **Using the Dockerized Version**

Alternatively, you can use the pre-built Docker image that includes the Kubling dialect:

1. Pull the Docker image:
   ```bash
   docker pull kubling/kubling-superset:latest
   ```

2. Run the container:
   ```bash
   docker run -d --name kubling-superset \
       -p 8088:8088 \
       -e SUPERSET_SECRET_KEY=$(openssl rand -base64 42) \
       kubling/kubling-superset:latest
   ```

   - Replace `$(openssl rand -base64 42)` with a strong secret key or leave it as-is for dynamic generation.

3. Access Superset:
   - Open your browser and go to [http://localhost:8088](http://localhost:8088).

4. Add a new database connection in Superset:
   - Use the same connection string format as described above:
     ```
     kubling://<username>:<password>@<host>:<port>/<vdb_name>
     ```

---

## 📝 Example

Here’s an example of how to use the dialect programmatically with SQLAlchemy:

```python
from sqlalchemy import create_engine

# Replace with your Kubling connection details
engine = create_engine("kubling://username:password@localhost:35432/vdb_name")

# Test a query
with engine.connect() as connection:
    result = connection.execute("SELECT * FROM SYS.SCHEMAS")
    for row in result:
        print(row)
```

---

## 🤝 Contributing

We welcome contributions to make this project better!

For major changes, please open an issue first to discuss what you would like to change.