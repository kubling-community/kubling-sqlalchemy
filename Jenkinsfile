#!groovy
import java.time.Instant

pipeline {
    agent any
    options { timestamps () }
    parameters {
        choice(name: 'CACHE', choices: ['', '--no-cache'], description: 'Build context cache')
    }
    stages {
        stage('Build and Publish') {
            environment {
                DOCKERHUB_CREDS = credentials('kubling-dockerhub')
                PYPI_CREDS = credentials('kubling-pypi')
            }
            steps {
                script {
                    env.NOW_SECONDS = Instant.now().getEpochSecond()
                }
                sh """
                  docker build --build-arg TWINE_USERNAME="$PYPI_CREDS_USR" --build-arg TWINE_PASSWORD="$PYPI_CREDS_PSW" \\
                  -t sqlalchemy-kubling-dialect-builder .
                """
                sh """ docker image rm sqlalchemy-kubling-dialect-builder """
                sh """ docker system prune -f """
            }
        }
    }
}