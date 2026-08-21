## Scripts to be run by the agents to set up the service

1. **Check prerequisites**: 
    - docker
    - docker-compose

2. **Check user's input**: Validate the provided configuration details 
    - image name or url : required
    - service name : if not provided, generate a default name based on the image name
    - port : if not provided, check for available ports in a defualt range (e.g., 8000-9000)
    - example config : from a yaml template or the documentation (WEB SEARCH REQUIRED)
    - environment variables
    - service location : if not provided, create a default folder in the current directory

3. **Create a folder for the service**: 

4. **Generate compose file based on the user's input**: 
    Required fields:
        - service name
        - image name or url
        - port mapping
        - health check endpoint (if applicable)
    
    if no port provided, run check_available_ports() to find an available port in the default range (8000-9000) and use it for the service.
```yaml
services:
  <service_name>:
    image: <image_name_or_url>
    ports:
      - "<port>:<port>"
    environment:
      - <ENV_VAR1>=<value1>
      - <ENV_VAR2>=<value2>
    volumes:
      - <host_path>:<container_path>

```

5. **Run docker-compose up**: Start the service using the generated compose file
    - Command: `docker-compose up -d`

6. **Verify service status**: Check if the service is running successfully
    - Command: `docker ps` or `docker-compose ps`
    - Check the logs for any errors
        Command: `docker-compose logs <service_name>`
        If errors are found, provide troubleshooting steps or suggestions to fix the issues.